// ==UserScript==
// @name         Instagram Model Followers Scraper + DM Auto Search & Chat
// @namespace    http://tampermonkey.net/
// @version      3.4
// @description  Enter model name, auto-navigate, scrape followers, go to DMs, search & message - fully automated with navigation reset
// @author       BEY
// @match        https://www.instagram.com/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=instagram.com
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      api.telegram.org
// ==/UserScript==

(function() {
    'use strict';

    // ==================== CONFIGURATION ====================
    const CONFIG = {
        DEFAULT_TARGET_COUNT: 50,
        SCROLL_DELAY_MS: 2000,
        MAX_SCROLL_ATTEMPTS: 30,
        AUTO_SEARCH_DELAY: 2000,
        CLICK_DELAY: 1000,
        PAGE_LOAD_DELAY: 4000,
        MODAL_WAIT_DELAY: 3000,
        MESSAGE_SEND_DELAY: 1200,
        DM_BETWEEN_USERS_DELAY_MS: 2000,
        DM_REQUIRE_APPROVAL: false,
        TELEGRAM_NOTIFY_EVERY_DMS: 10,
        USE_TELEGRAM: true,
        TELEGRAM_BOT_TOKEN: '8671289565:AAFxbYRSVvPkFRUaymh2T7BG6hyE-oIXXnE',
        COOLDOWN_THRESHOLD: 100,
        COOLDOWN_DURATION_MS: 10 * 60 * 1000,
        DEFAULT_MESSAGE_TEMPLATES: [
            'wait do i know u from somewhere??'
        ]
    };

    const STORAGE_KEYS = {
        ALL_PROCESSED_FOLLOWERS: 'ig_auto_dm_all_processed_followers_v1',
        SENT_COUNT_THIS_RUN: 'ig_auto_dm_sent_count_this_run_v1',
        LAST_NOTIFIED_MILESTONE: 'ig_auto_dm_last_notified_milestone_v1',
        DAILY_SUMMARY_STATS: 'ig_auto_dm_daily_summary_v1'
    };

    // ==================== STATE MANAGEMENT ====================
    const STATE = {
        modelName: null,
        isScraping: false,
        dmStopRequested: false,
        currentDMIndex: -1,
        isNavigatingForDM: false,
        runConfig: null,
        lastRecoveryAt: 0
    };

    // ==================== UTILITY FUNCTIONS ====================
    const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));

    const log = (msg, type = 'info') => {
        const prefixes = {
            info: '📋',
            success: '✅',
            error: '❌',
            warning: '⚠️',
            search: '🔍',
            scroll: '🔄',
            stats: '📊'
        };
        console.log(`${prefixes[type] || '•'} ${msg}`);
    };

    function setScraperButtonState(isRunning) {
        const button = document.getElementById('ig-follower-scraper-btn');
        if (!button) return;

        if (isRunning) {
            button.textContent = '⛔ Stop Bot';
            button.style.background = 'linear-gradient(45deg, #ff4d4d, #e11d48)';
        } else {
            button.textContent = '🚀 Start Bot';
            button.style.background = 'linear-gradient(45deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888)';
        }
    }

    function setScrapingState(isRunning) {
        STATE.isScraping = isRunning;
        setScraperButtonState(isRunning);
    }

    function restartFlowFromStart(reason = 'Unknown error') {
        const now = Date.now();
        if (now - STATE.lastRecoveryAt < 3000) {
            log(`Recovery throttled: ${reason}`, 'warning');
            return;
        }
        STATE.lastRecoveryAt = now;

        const runConfig = getCurrentRunConfig();
        const modelName = (runConfig && runConfig.modelName) || STATE.modelName;
        if (!modelName) {
            log(`Recovery failed (no model configured): ${reason}`, 'error');
            setScrapingState(false);
            return;
        }

        STATE.modelName = modelName;
        STATE.dmStopRequested = false;
        STATE.isNavigatingForDM = true;
        setScrapingState(true);

        sessionStorage.setItem('scraper_state', JSON.stringify({
            step: 'navigate_to_profile',
            modelName: modelName
        }));
        sessionStorage.removeItem('auto_dm_queue');
        sessionStorage.removeItem('dm_queue_index');

        log(`Recovery triggered: ${reason}. Restarting from @${modelName} profile...`, 'warning');
        setTimeout(() => navigateToProfile(modelName), 1200);
    }

    async function stopScraperFlow() {
        const runConfig = getCurrentRunConfig();
        const totalSent = getSentCountThisRun();
        const senderName = (runConfig && runConfig.senderUsername) || 'Unknown';
        const targetModel = (runConfig && runConfig.modelName) || 'Unknown';
        await sendTelegramNotification(`${senderName}\nTarget: @${targetModel}\nBot stopped. Total DMs sent: ${totalSent}.`, runConfig);

        log('Stopping scraper flow...', 'warning');
        STATE.modelName = null;
        STATE.runConfig = null;
        STATE.dmStopRequested = true;
        STATE.currentDMIndex = -1;
        STATE.isNavigatingForDM = false;
        sessionStorage.removeItem('scraper_state');
        sessionStorage.removeItem('scraper_config');
        sessionStorage.removeItem('auto_dm_queue');
        sessionStorage.removeItem('dm_queue_index');
        sessionStorage.removeItem('scrape_offset');
        sessionStorage.removeItem('scrapedFollowers');
        sessionStorage.removeItem('dmProcessedFollowers');
        setScrapingState(false);
    }

    function getProcessedFollowers() {
        try {
            const raw = sessionStorage.getItem('dmProcessedFollowers');
            const parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed : [];
        } catch (err) {
            return [];
        }
    }

    function getAllProcessedFollowers() {
        try {
            const raw = localStorage.getItem(STORAGE_KEYS.ALL_PROCESSED_FOLLOWERS);
            const parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed : [];
        } catch (err) {
            return [];
        }
    }

    function saveAllProcessedFollower(username) {
        if (!username) return;
        const normalizedUsername = normalizeUsername(username);
        if (!normalizedUsername) return;

        const allProcessed = getAllProcessedFollowers();
        if (!allProcessed.includes(normalizedUsername)) {
            allProcessed.push(normalizedUsername);
            localStorage.setItem(STORAGE_KEYS.ALL_PROCESSED_FOLLOWERS, JSON.stringify(allProcessed));
        }
    }

    function hasEverProcessedFollower(username) {
        if (!username) return false;
        const normalizedUsername = normalizeUsername(username);
        if (!normalizedUsername) return false;
        return getAllProcessedFollowers().includes(normalizedUsername);
    }

    function saveProcessedFollower(username) {
        if (!username) return;
        const normalizedUsername = normalizeUsername(username);
        if (!normalizedUsername) return;

        const processed = getProcessedFollowers();
        if (!processed.includes(normalizedUsername)) {
            processed.push(normalizedUsername);
            sessionStorage.setItem('dmProcessedFollowers', JSON.stringify(processed));
        }

        saveAllProcessedFollower(normalizedUsername);
    }

    function getPendingFollowers() {
        try {
            const scraped = JSON.parse(sessionStorage.getItem('scrapedFollowers') || '[]');
            const processedThisRun = new Set(getProcessedFollowers());
            const processedAllRuns = new Set(getAllProcessedFollowers());
            return scraped.filter(username => {
                const normalizedUsername = normalizeUsername(username);
                return normalizedUsername && !processedThisRun.has(normalizedUsername) && !processedAllRuns.has(normalizedUsername);
            });
        } catch (err) {
            return [];
        }
    }

    function shouldAutoRunDMQueue() {
        return sessionStorage.getItem('auto_dm_queue') === '1';
    }

    function sanitizeTargetCount(rawValue) {
        const parsed = parseInt(String(rawValue || '').trim(), 10);
        if (!Number.isFinite(parsed)) return CONFIG.DEFAULT_TARGET_COUNT;
        return Math.max(1, Math.min(500, parsed));
    }

    function sanitizeTemplates(rawValue) {
        const fromInput = String(rawValue || '')
            .split('\n')
            .map(line => line.trim())
            .filter(Boolean);
        return fromInput.length > 0 ? fromInput : [...CONFIG.DEFAULT_MESSAGE_TEMPLATES];
    }

    function sanitizeTelegramChatIdList(rawValue) {
        const values = String(rawValue || '')
            .split(/[\n,\s]+/)
            .map(value => value.trim())
            .filter(Boolean);

        return Array.from(new Set(values)).join(', ');
    }

    function parseTelegramChatIds(rawValue) {
        return String(rawValue || '')
            .split(/[\n,\s]+/)
            .map(value => value.trim())
            .filter(Boolean);
    }

    function getSentCountThisRun() {
        const runConfig = getCurrentRunConfig();
        const username = (runConfig && runConfig.senderUsername) || 'default';
        const key = `${STORAGE_KEYS.SENT_COUNT_THIS_RUN}_${username}`;
        const raw = localStorage.getItem(key);
        const parsed = raw ? parseInt(raw, 10) : 0;
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
    }

    function setSentCountThisRun(value) {
        const runConfig = getCurrentRunConfig();
        const username = (runConfig && runConfig.senderUsername) || 'default';
        const key = `${STORAGE_KEYS.SENT_COUNT_THIS_RUN}_${username}`;
        const safeValue = Number.isFinite(value) ? Math.max(0, value) : 0;
        localStorage.setItem(key, String(safeValue));
    }

    function incrementSentCountThisRun() {
        const next = getSentCountThisRun() + 1;
        setSentCountThisRun(next);
        
        const runConfig = getCurrentRunConfig();
        if (runConfig && runConfig.senderUsername) {
            processDailySummary(runConfig.senderUsername, 1);
        }
        
        return next;
    }

    function processDailySummary(senderUsername, increment = 0) {
        if (!senderUsername) return;
        
        const key = STORAGE_KEYS.DAILY_SUMMARY_STATS;
        let stats = {};
        try {
            stats = JSON.parse(localStorage.getItem(key) || '{}');
        } catch(e) {}
        
        const now = Date.now();
        if (!stats[senderUsername]) {
            stats[senderUsername] = { startTime: now, count: 0 };
        }
        
        stats[senderUsername].count += increment;
        
        const twentyFourHours = 24 * 60 * 60 * 1000;
        if (now - stats[senderUsername].startTime >= twentyFourHours) {
            const total = stats[senderUsername].count;
            const runConfig = getCurrentRunConfig();
            sendTelegramNotification(`${senderUsername}\n24-Hour Report: Total ${total} DMs sent.`, runConfig);
            
            // Reset for next period
            stats[senderUsername].startTime = now;
            stats[senderUsername].count = 0;
        }
        
        localStorage.setItem(key, JSON.stringify(stats));
    }

    function getLastNotifiedMilestone() {
        const runConfig = getCurrentRunConfig();
        const username = (runConfig && runConfig.senderUsername) || 'default';
        const key = `${STORAGE_KEYS.LAST_NOTIFIED_MILESTONE}_${username}`;
        const raw = localStorage.getItem(key);
        const parsed = raw ? parseInt(raw, 10) : 0;
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
    }

    function setLastNotifiedMilestone(value) {
        const runConfig = getCurrentRunConfig();
        const username = (runConfig && runConfig.senderUsername) || 'default';
        const key = `${STORAGE_KEYS.LAST_NOTIFIED_MILESTONE}_${username}`;
        const safeValue = Number.isFinite(value) ? Math.max(0, value) : 0;
        localStorage.setItem(key, String(safeValue));
    }

    function getTelegramChatIds(runConfig = null) {
        const fromConfig = runConfig && runConfig.telegramChatIds
            ? parseTelegramChatIds(runConfig.telegramChatIds)
            : [];
        if (fromConfig.length > 0) return fromConfig;

        const currentConfig = getCurrentRunConfig();
        if (currentConfig && currentConfig.telegramChatIds) {
            return parseTelegramChatIds(currentConfig.telegramChatIds);
        }

        return [];
    }

    function sendTelegramNotification(message, runConfig = null, useLockBot = false) {
        let token = (CONFIG.TELEGRAM_BOT_TOKEN || '').trim();
        if (useLockBot && runConfig && runConfig.lockAlertBotToken) {
            token = runConfig.lockAlertBotToken.trim();
        }
        const chatIds = getTelegramChatIds(runConfig);
        
        // Check if Telegram is disabled via toggle
        const useTelegram = runConfig ? runConfig.useTelegram : CONFIG.USE_TELEGRAM;
        if (!useTelegram) {
            return Promise.resolve();
        }

        if (!token) {
            log('Telegram notification skipped: No bot token configured.', 'warning');
            return Promise.resolve();
        }

        if (chatIds.length === 0) {
            return Promise.resolve();
        }

        if (!message) return Promise.resolve();

        const useGmxhr = typeof GM_xmlhttpRequest !== 'undefined';
        log(`Sending Telegram notification to ${chatIds.length} chat(s)...`, 'info');

        const promises = chatIds.map(chatId => {
            const payload = {
                chat_id: chatId,
                text: message
            };

            if (useGmxhr) {
                return new Promise((resolve) => {
                    GM_xmlhttpRequest({
                        method: 'POST',
                        url: `https://api.telegram.org/bot${token}/sendMessage`,
                        headers: {
                            'Content-Type': 'application/json'
                        },
                        data: JSON.stringify(payload),
                        onload: (response) => {
                            try {
                                const res = JSON.parse(response.responseText);
                                if (res.ok) {
                                    log(`✅ Telegram notification sent to ${chatId}`, 'success');
                                } else {
                                    log(`❌ Telegram error (${chatId}): ${res.description || 'Unknown error'}`, 'error');
                                }
                            } catch (e) {
                                log(`❌ Telegram response parse error (${chatId})`, 'error');
                            }
                            resolve();
                        },
                        onerror: (err) => {
                            log(`❌ Telegram request failed for ${chatId}: ${err.statusText || 'Network error'}`, 'error');
                            resolve();
                        },
                        ontimeout: () => {
                            log(`❌ Telegram request timed out for ${chatId}`, 'error');
                            resolve();
                        }
                    });
                });
            } else {
                return fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                })
                .then(r => r.json())
                .then(res => {
                    if (res.ok) log(`✅ Telegram notification sent (fetch) to ${chatId}`, 'success');
                    else log(`❌ Telegram error (fetch): ${res.description}`, 'error');
                })
                .catch(() => log(`❌ Telegram fetch failed for ${chatId}`));
            }
        });

        return Promise.all(promises);
    }

    function getCurrentRunConfig() {
        if (STATE.runConfig && STATE.runConfig.modelName) {
            return STATE.runConfig;
        }

        try {
            const saved = JSON.parse(sessionStorage.getItem('scraper_config') || 'null');
            if (saved && saved.modelName) {
                STATE.runConfig = {
                    senderUsername: String(saved.senderUsername || '').trim().replace('@', ''),
                    modelName: String(saved.modelName).trim().replace('@', ''),
                    targetCount: sanitizeTargetCount(saved.targetCount),
                    useTelegram: saved.useTelegram !== false, // default to true
                    messageTemplates: sanitizeTemplates((saved.messageTemplates || []).join('\n')),
                    telegramChatIds: sanitizeTelegramChatIdList(saved.telegramChatIds || saved.telegramChatId)
                };
                return STATE.runConfig;
            }
        } catch (err) {
            return null;
        }

        return null;
    }

    function saveRunConfig(config) {
        STATE.runConfig = {
            senderUsername: String(config.senderUsername || '').trim().replace('@', ''),
            modelName: config.modelName,
            targetCount: sanitizeTargetCount(config.targetCount),
            useTelegram: config.useTelegram !== false,
            messageTemplates: sanitizeTemplates((config.messageTemplates || []).join('\n')),
            telegramChatIds: sanitizeTelegramChatIdList(config.telegramChatIds || config.telegramChatId)
        };
        sessionStorage.setItem('scraper_config', JSON.stringify(STATE.runConfig));
    }

    function getScrapeOffset() {
        const raw = sessionStorage.getItem('scrape_offset');
        const parsed = raw ? parseInt(raw, 10) : 0;
        return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
    }

    function setScrapeOffset(offset) {
        const safeOffset = Number.isFinite(offset) ? Math.max(0, offset) : 0;
        sessionStorage.setItem('scrape_offset', String(safeOffset));
    }

    function getSavedDMQueueIndex() {
        const idx = sessionStorage.getItem('dm_queue_index');
        return idx ? parseInt(idx, 10) : -1;
    }

    function saveDMQueueIndex(index) {
        sessionStorage.setItem('dm_queue_index', index.toString());
    }

    // ==================== UI COMPONENTS ====================
    function addScraperButton() {
        if (document.getElementById('ig-follower-scraper-btn')) return;

        const button = document.createElement('button');
        button.id = 'ig-follower-scraper-btn';
        button.textContent = '🚀 Start Scraper';
        button.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 9999;
            background: linear-gradient(45deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888);
            color: white;
            border: none;
            border-radius: 8px;
            padding: 12px 24px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            transition: transform 0.2s;
        `;

        button.addEventListener('mouseenter', () => button.style.transform = 'scale(1.05)');
        button.addEventListener('mouseleave', () => button.style.transform = 'scale(1)');
        button.addEventListener('click', () => {
            if (STATE.isScraping) {
                stopScraperFlow();
                return;
            }
            startScraperFlow();
        });

        document.body.appendChild(button);
        setScraperButtonState(STATE.isScraping);
    }

    function promptForRunConfig() {
        return new Promise((resolve) => {
            const existingConfig = getCurrentRunConfig() || {
                senderUsername: '',
                modelName: '',
                targetCount: CONFIG.DEFAULT_TARGET_COUNT,
                useTelegram: CONFIG.USE_TELEGRAM,
                messageTemplates: [...CONFIG.DEFAULT_MESSAGE_TEMPLATES],
                telegramChatIds: ''
            };

            const overlay = document.createElement('div');
            overlay.style.cssText = `
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background: rgba(0,0,0,0.7);
                z-index: 99999;
                display: flex;
                align-items: center;
                justify-content: center;
            `;

            const dialog = document.createElement('div');
            dialog.style.cssText = `
                background: white;
                border-radius: 12px;
                padding: 30px;
                width: 460px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.3);
                text-align: center;
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            `;

            dialog.innerHTML = `
                <h2 style="margin: 0 0 10px 0; color: #262626; font-size: 20px;">🤖 Instagram Auto Scraper</h2>
                <p style="color: #8e8e8e; margin: 0 0 20px 0; font-size: 14px;">Configure accounts, follower batch size, and message templates</p>
                
                <div style="margin-bottom: 20px; text-align: left;">
                    <label for="sender-name-input" style="display: block; margin-bottom: 6px; color: #262626; font-size: 13px; font-weight: 600;">Your IG Username (Sender)</label>
                    <div style="position: relative;">
                        <span style="position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: #8e8e8e;">@</span>
                        <input id="sender-name-input" type="text" placeholder="your_username" style="
                            width: 100%;
                            padding: 12px 12px 12px 30px;
                            border: 1px solid #dbdbdb;
                            color: #111;
                            caret-color: #111;
                            border-radius: 6px;
                            font-size: 16px;
                            box-sizing: border-box;
                            outline: none;
                        " value="${existingConfig.senderUsername || ''}">
                    </div>
                </div>

                <div style="margin-bottom: 20px; text-align: left;">
                    <label for="model-name-input" style="display: block; margin-bottom: 6px; color: #262626; font-size: 13px; font-weight: 600;">Target Model Username</label>
                    <div style="position: relative;">
                        <span style="position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: #8e8e8e;">@</span>
                        <input id="model-name-input" type="text" placeholder="model_username" style="
                            width: 100%;
                            padding: 12px 12px 12px 30px;
                            border: 1px solid #dbdbdb;
                            color: #111;
                            caret-color: #111;
                            border-radius: 6px;
                            font-size: 16px;
                            box-sizing: border-box;
                            outline: none;
                        " value="${existingConfig.modelName}" autofocus>
                    </div>
                </div>
                <div style="margin-bottom: 20px; text-align: left;">
                    <label for="followers-count-input" style="display: block; margin-bottom: 6px; color: #262626; font-size: 13px; font-weight: 600;">Followers to scrape per batch</label>
                    <input id="followers-count-input" type="number" min="1" max="500" value="${existingConfig.targetCount}" style="
                        width: 100%;
                        padding: 12px;
                        border: 1px solid #dbdbdb;
                        color: #111;
                        caret-color: #111;
                        border-radius: 6px;
                        font-size: 15px;
                        box-sizing: border-box;
                        outline: none;
                    ">
                </div>
                <div style="margin-bottom: 20px; text-align: left;">
                    <label for="message-templates-input" style="display: block; margin-bottom: 6px; color: #262626; font-size: 13px; font-weight: 600;">Message templates (one per line)</label>
                    <textarea id="message-templates-input" rows="5" style="
                        width: 100%;
                        padding: 12px;
                        border: 1px solid #dbdbdb;
                        color: #111;
                        caret-color: #111;
                        border-radius: 6px;
                        font-size: 14px;
                        line-height: 1.4;
                        box-sizing: border-box;
                        outline: none;
                        resize: vertical;
                    ">${existingConfig.messageTemplates.join('\n')}</textarea>
                </div>
                <div style="margin-bottom: 20px; text-align: left;">
                    <label for="telegram-chat-id-input" style="display: block; margin-bottom: 6px; color: #262626; font-size: 13px; font-weight: 600;">Telegram Chat IDs (comma or new line)</label>
                    <input id="telegram-chat-id-input" type="text" placeholder="e.g. 123456789, 987654321" value="${existingConfig.telegramChatIds || ''}" style="
                        width: 100%;
                        padding: 12px;
                        border: 1px solid #dbdbdb;
                        color: #111;
                        caret-color: #111;
                        border-radius: 6px;
                        font-size: 15px;
                        box-sizing: border-box;
                        outline: none;
                    ">
                </div>
                <div style="margin-bottom: 20px; text-align: left; display: flex; align-items: center; gap: 10px;">
                    <input id="use-telegram-checkbox" type="checkbox" ${existingConfig.useTelegram ? 'checked' : ''} style="width: 18px; height: 18px; cursor: pointer;">
                    <label for="use-telegram-checkbox" style="color: #262626; font-size: 14px; font-weight: 600; cursor: pointer;">Enable Telegram Notifications</label>
                </div>
                <div style="display: flex; gap: 10px; justify-content: center;">
                    <button id="cancel-btn" style="
                        background: #efefef;
                        color: #262626;
                        border: none;
                        border-radius: 8px;
                        padding: 10px 20px;
                        font-size: 14px;
                        font-weight: 600;
                        cursor: pointer;
                    ">Cancel</button>
                    <button id="start-btn" style="
                        background: #0095f6;
                        color: white;
                        border: none;
                        border-radius: 8px;
                        padding: 10px 20px;
                        font-size: 14px;
                        font-weight: 600;
                        cursor: pointer;
                    ">Start Auto Scraping</button>
                </div>
            `;

            overlay.appendChild(dialog);
            document.body.appendChild(overlay);

            const input = dialog.querySelector('#model-name-input');
            const followersCountInput = dialog.querySelector('#followers-count-input');
            const messageTemplatesInput = dialog.querySelector('#message-templates-input');
            const telegramChatIdInput = dialog.querySelector('#telegram-chat-id-input');
            const startBtn = dialog.querySelector('#start-btn');
            const cancelBtn = dialog.querySelector('#cancel-btn');

            const cleanup = () => overlay.remove();

            startBtn.addEventListener('click', () => {
                const senderUsername = dialog.querySelector('#sender-name-input').value.trim().replace('@', '');
                const modelName = input.value.trim().replace('@', '');
                const targetCount = sanitizeTargetCount(followersCountInput.value);
                const useTelegram = dialog.querySelector('#use-telegram-checkbox').checked;
                const messageTemplates = sanitizeTemplates(messageTemplatesInput.value);
                const telegramChatIds = sanitizeTelegramChatIdList(telegramChatIdInput.value);

                if (modelName) {
                    cleanup();
                    resolve({ senderUsername, modelName, targetCount, useTelegram, messageTemplates, telegramChatIds });
                } else {
                    input.style.borderColor = '#ed4956';
                    input.placeholder = 'Please enter a username';
                }
            });

            cancelBtn.addEventListener('click', () => {
                cleanup();
                resolve(null);
            });

            input.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') startBtn.click();
            });

            setTimeout(() => input.focus(), 100);
        });
    }

    // ==================== NAVIGATION FUNCTIONS ====================
    function navigateToProfile(modelName) {
        log(`Navigating to https://www.instagram.com/${modelName}/`);
        window.location.href = `https://www.instagram.com/${modelName}/`;
    }

    function navigateToInbox() {
        log('Navigating back to Direct Messages inbox...', 'info');
        window.location.href = 'https://www.instagram.com/direct/inbox/';
    }

    async function dismissPopups() {
        const buttons = document.querySelectorAll('button');
        for (const btn of buttons) {
            const text = btn.innerText.toLowerCase();
            if (text === 'not now' || text === 'maybe later') {
                log(`Dismissing popup: ${text}`, 'info');
                btn.click();
                await wait(1000);
            }
        }
    }

    // ==================== FOLLOWERS MODAL FUNCTIONS ====================
    async function clickFollowersLinkSimple() {
        log('Looking for followers link...', 'search');
        await wait(3000); // Give it a bit more time

        // Try multiple selectors
        const selectors = [
            'a[href*="/followers/"]',
            'a[href$="/followers/"]',
            'header a[href*="followers"]',
            'a[role="link"]'
        ];

        let link = null;
        for (const selector of selectors) {
            const elements = document.querySelectorAll(selector);
            for (const el of elements) {
                const text = (el.innerText || el.textContent || '').toLowerCase();
                const href = el.getAttribute('href') || '';
                if (text.includes('follower') || href.includes('/followers/')) {
                    link = el;
                    break;
                }
            }
            if (link) break;
        }

        if (link) {
            log('Found followers link, clicking...', 'success');
            link.click();
            
            // Fallback: If click doesn't work, try dispatching event
            await wait(1000);
            const dialog = document.querySelector('div[role="dialog"]');
            if (!dialog) {
                link.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            }

            await wait(3000);

            if (document.querySelector('div[role="dialog"]')) {
                log('Followers modal opened successfully!', 'success');
                return true;
            }
        }

        log('Failed to open followers modal using standard selectors', 'error');
        return false;
    }

    // ==================== SCRAPING FUNCTIONS ====================
    function extractUsernamesFromDOM() {
        const usernames = new Set();
        const selectors = [
            'div[role="dialog"] a[href^="/"][href*="/"]:not([href*="/explore/"]):not([href*="/accounts/"])',
            'div[role="presentation"] a[href^="/"]',
            '._aano a[href^="/"]',
            'div[style*="height"] a[href^="/"]',
            'a[href^="/"][class*="x1i10hfl"]'
        ];

        for (const selector of selectors) {
            const links = document.querySelectorAll(selector);
            links.forEach(link => {
                const href = link.getAttribute('href');
                if (href && href !== '/' && href.length > 1 && !href.includes('?')) {
                    let username = href.replace(/^\//, '').replace(/\/$/, '');
                    if (username && !username.includes('/') && !username.includes('#')) {
                        if (!['accounts', 'explore', 'p', 'reel', 'stories'].includes(username)) {
                            usernames.add(username);
                        }
                    }
                }
            });
        }
        return Array.from(usernames);
    }

    async function findScrollContainer() {
        const dialog = document.querySelector('div[role="dialog"]');
        if (!dialog) return null;

        const scrollable = dialog.querySelector('div[style*="overflow-y: auto"], div[style*="overflow: auto"], div[style*="overflow-y: scroll"]');
        if (scrollable) return scrollable;

        const allElements = dialog.querySelectorAll('*');
        for (const el of allElements) {
            if (el.scrollHeight > el.clientHeight + 10) {
                return el;
            }
        }

        return dialog;
    }

    async function triggerLoadMore() {
        const buttons = document.querySelectorAll('div[role="dialog"] button:not([disabled]), div[role="dialog"] [role="button"]');
        for (const button of buttons) {
            if (button.innerText && (button.innerText.includes('Load') || button.innerText.includes('See') || button.innerText.includes('More'))) {
                button.click();
                return true;
            }
        }
        return false;
    }

    async function scrapeFollowersFromModal(targetCount, startOffset) {
        if (!STATE.isScraping) {
            log('Scraping was stopped before modal processing started', 'warning');
            return { result: [], uniqueUsernames: [], finalUsernames: [] };
        }

        log('Checking for followers modal...', 'search');
        await wait(2000);

        const dialog = document.querySelector('div[role="dialog"]');
        if (!dialog) {
            log('Followers modal not found', 'error');
            return { result: [], uniqueUsernames: [], finalUsernames: [] };
        }

        log('Followers modal detected', 'success');
        const requiredUniqueCount = startOffset + targetCount;
        log(`Batch target: ${targetCount} usernames (offset: ${startOffset})`);

        let scrollContainer = await findScrollContainer();
        if (!scrollContainer) {
            scrollContainer = dialog;
            log('Using dialog as scroll container', 'warning');
        }
        log('Scroll container ready', 'success');

        let previousUsernames = [];
        let stagnantCount = 0;
        let scrollAttempts = 0;

        while (scrollAttempts < CONFIG.MAX_SCROLL_ATTEMPTS) {
            if (!STATE.isScraping) {
                log('Scraping stopped by user', 'warning');
                break;
            }

            const currentUsernames = extractUsernamesFromDOM();
            const uniqueUsernames = [...new Set(currentUsernames)];

            log(`Found ${currentUsernames.length} elements (${uniqueUsernames.length} unique)`, 'stats');

            if (currentUsernames.length > 0 && currentUsernames.length !== previousUsernames.length) {
                log(`Latest: ${currentUsernames.slice(-3).join(', ')}`);
            }

            if (uniqueUsernames.length >= requiredUniqueCount) {
                log(`Target reached! ${uniqueUsernames.length} usernames`, 'success');
                break;
            }

            if (currentUsernames.length === previousUsernames.length) {
                stagnantCount++;
                if (stagnantCount >= 4) {
                    log(`No new users after ${stagnantCount} attempts`, 'warning');
                    const loaded = await triggerLoadMore();
                    if (!loaded) {
                        log('Reached end of followers list', 'warning');
                        break;
                    }
                    stagnantCount = 0;
                }
            } else {
                stagnantCount = 0;
            }

            previousUsernames = [...currentUsernames];
            log(`Scrolling... (${scrollAttempts + 1}/${CONFIG.MAX_SCROLL_ATTEMPTS})`, 'scroll');

            for (let i = 0; i < 3; i++) {
                scrollContainer.scrollTop = scrollContainer.scrollHeight;
                await wait(300);
            }

            await wait(CONFIG.SCROLL_DELAY_MS);
            scrollAttempts++;
        }

        const finalUsernames = extractUsernamesFromDOM();
        const uniqueUsernames = [...new Set(finalUsernames)];
        const result = uniqueUsernames.slice(startOffset, startOffset + targetCount);

        return { result, uniqueUsernames, finalUsernames };
    }

    // ==================== DM FUNCTIONS ====================
    async function clickNewMessageButton() {
        log('Looking for "New message" button...', 'search');
        await wait(2000);

        // Strategy 1: By SVG aria-label
        const svgWithTitle = document.querySelector('svg[aria-label="New message"]');
        if (svgWithTitle) {
            const clickable = svgWithTitle.closest('a, button, [role="button"], div[role="button"]');
            if (clickable) {
                log('Found New message button', 'success');
                clickable.click();
                return true;
            }
            svgWithTitle.click();
            return true;
        }

        // Strategy 2: By SVG path
        const svgElements = document.querySelectorAll('svg');
        for (const svg of svgElements) {
            const path = svg.querySelector('path[d*="M12.202 3.203H5.25"]');
            if (path) {
                const clickable = svg.closest('a, button, [role="button"], div[role="button"]');
                if (clickable) {
                    log('Found New message button via path', 'success');
                    clickable.click();
                    return true;
                }
                svg.click();
                return true;
            }
        }

        // Strategy 3: By aria-label
        const labeledElements = document.querySelectorAll('[aria-label="New message"]');
        for (const el of labeledElements) {
            el.click();
            log('Found New message via aria-label', 'success');
            return true;
        }

        log('New message button not found', 'error');
        return false;
    }

    async function searchForUser(username) {
        log(`Searching for user: ${username}`, 'search');
        await wait(CONFIG.AUTO_SEARCH_DELAY);

        const searchInput = document.querySelector('input[name="queryBox"]');
        if (!searchInput) {
            log('Search input not found, retrying...', 'warning');
            await wait(1000);
            const retryInput = document.querySelector('input[name="queryBox"]');
            if (!retryInput) {
                log('Search input still not found', 'error');
                return false;
            }
            return await typeInInput(retryInput, username);
        }

        return await typeInInput(searchInput, username);
    }

    async function typeInInput(inputElement, text) {
        inputElement.focus();
        await wait(300);

        const targetWindow = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(targetWindow.HTMLInputElement.prototype, 'value').set;

        // Clear existing value
        nativeInputValueSetter.call(inputElement, '');
        inputElement.dispatchEvent(new Event('input', { bubbles: true }));
        inputElement.dispatchEvent(new Event('change', { bubbles: true }));
        await wait(300);

        // Type each character
        for (let i = 0; i < text.length; i++) {
            const char = text[i];
            const currentValue = inputElement.value;

            nativeInputValueSetter.call(inputElement, currentValue + char);

            inputElement.dispatchEvent(new Event('input', { bubbles: true }));
            inputElement.dispatchEvent(new Event('change', { bubbles: true }));
            inputElement.dispatchEvent(new KeyboardEvent('keydown', { key: char, bubbles: true }));
            inputElement.dispatchEvent(new KeyboardEvent('keypress', { key: char, bubbles: true }));
            inputElement.dispatchEvent(new KeyboardEvent('keyup', { key: char, bubbles: true }));

            await wait(80);
        }

        log(`Typed: ${text}`, 'success');

        inputElement.dispatchEvent(new Event('input', { bubbles: true }));
        inputElement.dispatchEvent(new Event('change', { bubbles: true }));
        inputElement.dispatchEvent(new Event('blur', { bubbles: true }));

        await wait(2000);
        return true;
    }

    function normalizeUsername(value) {
        return (value || '').trim().replace(/^@+/, '').toLowerCase();
    }

    function extractUsernameFromProfileHref(href) {
        if (!href || !href.startsWith('/')) return null;
        const cleaned = href.replace(/^\//, '').replace(/\/$/, '');
        if (!cleaned || cleaned.includes('/') || cleaned.includes('?')) return null;
        const blocked = new Set(['direct', 'accounts', 'explore', 'reels', 'stories', 'p']);
        return blocked.has(cleaned.toLowerCase()) ? null : cleaned;
    }

    function isConversationForUsernameOpen(username) {
        const normalizedTarget = normalizeUsername(username);
        if (!normalizedTarget) return false;

        const headerLinks = document.querySelectorAll('main header a[href^="/"]');
        for (const link of headerLinks) {
            const profileUsername = extractUsernameFromProfileHref(link.getAttribute('href'));
            if (normalizeUsername(profileUsername) === normalizedTarget) {
                return true;
            }
        }

        const headerTextCandidates = document.querySelectorAll('main header h1, main header h2, main header span, main header div');
        for (const el of headerTextCandidates) {
            const text = normalizeUsername((el.innerText || el.textContent || '').split('\n')[0]);
            if (text === normalizedTarget) {
                return true;
            }
        }

        return false;
    }

    function getActiveThreadIdFromUrl() {
        const match = window.location.pathname.match(/\/direct\/t\/([^\/?#]+)/i);
        return match ? match[1] : null;
    }

    function isComposeDialogOpen() {
        const dialog = document.querySelector('div[role="dialog"]');
        if (!dialog) return false;
        const hasQueryBox = !!dialog.querySelector('input[name="queryBox"]');
        const hasListbox = !!dialog.querySelector('div[role="listbox"]');
        return hasQueryBox || hasListbox;
    }

    function hasMessageRequestSentNotice(root = document) {
        const requestHelpLink = root.querySelector('a[href*="help.instagram.com/1435783229983256"], a[aria-label*="Learn more about message requests"]');
        if (requestHelpLink) {
            return true;
        }

        const nodes = root.querySelectorAll('span, div');
        for (const node of nodes) {
            const text = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
            if (text.includes('message request sent') || text.includes('you can send more messages after they accept')) {
                return true;
            }
        }
        return false;
    }

    async function waitForConversationOpen(username, previousThreadId = null, timeoutMs = 7000) {
        const started = Date.now();
        while (Date.now() - started < timeoutMs) {
            const currentThreadId = getActiveThreadIdFromUrl();
            const threadChanged = !!currentThreadId && !!previousThreadId && currentThreadId !== previousThreadId;
            const composerReady = !!document.querySelector('div[role="textbox"][contenteditable="true"], div[data-lexical-editor="true"][role="textbox"]');
            const composeClosed = !isComposeDialogOpen();

            if (threadChanged || isConversationForUsernameOpen(username) || (currentThreadId && composeClosed && composerReady)) {
                return true;
            }
            await wait(300);
        }
        return false;
    }

    async function selectSearchResultForUser(username) {
        log(`Looking for search result for @${username}...`, 'search');
        await wait(1500);

        const hasNoAccountFoundMessage = () => {
            const candidates = document.querySelectorAll('div[role="dialog"] span, div[role="dialog"] div, main span, main div');
            for (const el of candidates) {
                const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                if (text === 'no account found.' || text === 'no account found') {
                    return true;
                }
            }
            return false;
        };

        let options = [];
        const listbox = document.querySelector('div[role="listbox"]');
        if (listbox) {
            options = Array.from(listbox.querySelectorAll('div[role="option"]'));
        }
        
        if (options.length === 0) {
            const checkboxes = document.querySelectorAll('input[name="IGDRecipientContactSearchResultCheckbox"]');
            options = Array.from(checkboxes).map(cb => cb.closest('[role="button"]') || cb);
        }

        if (options.length === 0) {
            if (hasNoAccountFoundMessage()) {
                log(`No account found for @${username}. Skipping user.`, 'warning');
                return 'no_account';
            }
            log(`No search results found for @${username}. Skipping user.`, 'warning');
            return 'no_results';
        }

        const normalizedTarget = normalizeUsername(username);
        let selectedOption = null;

        for (const option of options) {
            const spans = option.querySelectorAll('span');
            let foundExactSpan = false;
            for (const span of spans) {
                if (normalizeUsername(span.innerText || span.textContent) === normalizedTarget) {
                    foundExactSpan = true;
                    break;
                }
            }
            if (foundExactSpan) {
                selectedOption = option;
                break;
            }

            const profileLink = option.querySelector('a[href^="/"]');
            const profileUsername = normalizeUsername(extractUsernameFromProfileHref(profileLink?.getAttribute('href')));
            if (profileUsername && profileUsername === normalizedTarget) {
                selectedOption = option;
                break;
            }

            const optionText = normalizeUsername(option.innerText || option.textContent);
            if (optionText === normalizedTarget || optionText.includes(`@${normalizedTarget}`) || optionText.includes(` ${normalizedTarget} `)) {
                selectedOption = option;
                break;
            }
        }

        if (!selectedOption) {
            // One final fallback: look through any element inside the option
            for (const option of options) {
                const allDescendants = option.querySelectorAll('*');
                let foundExactMatch = false;
                for (const desc of allDescendants) {
                    if (normalizeUsername(desc.innerText || desc.textContent) === normalizedTarget) {
                        foundExactMatch = true;
                        break;
                    }
                }
                if (foundExactMatch) {
                    selectedOption = option;
                    break;
                }
            }
        }

        if (!selectedOption) {
            log(`Exact match not found, falling back to first result for @${username}`, 'warning');
            selectedOption = options[0];
        }

        log(`Found ${options.length} results, selecting best match...`, 'success');
        
        // Before clicking, check if there's a specific checkbox to click inside this option
        const checkbox = selectedOption.querySelector('input[name="IGDRecipientContactSearchResultCheckbox"]');
        if (checkbox) {
            checkbox.click();
        } else {
            selectedOption.click();
        }
        await wait(CONFIG.CLICK_DELAY);
        return true;
    }

    async function clickChatButton() {
        log('Looking for "Chat" button...', 'search');
        
        const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim().toLowerCase();
        const isClickable = (el) => {
            if (!el) return false;
            if (el.getAttribute('aria-disabled') === 'true') return false;
            if ('disabled' in el && el.disabled) return false;
            return true;
        };

        const attemptClick = () => {
            const clickByText = (root, texts) => {
                const candidates = root.querySelectorAll('button, div[role="button"], a[role="button"]');
                for (const candidate of candidates) {
                    if (!isClickable(candidate)) continue;
                    const candidateText = normalize(candidate.innerText || candidate.textContent);
                    if (texts.includes(candidateText)) {
                        candidate.click();
                        return candidateText;
                    }
                }
                return null;
            };

            const dialog = document.querySelector('div[role="dialog"]');
            const searchRoots = dialog ? [dialog, document] : [document];

            for (const root of searchRoots) {
                const clickedText = clickByText(root, ['chat', 'next']);
                if (clickedText) {
                    log(`Found ${clickedText === 'chat' ? 'Chat' : 'Next'} button`, 'success');
                    return true;
                }
            }

            const ariaCandidates = document.querySelectorAll('[aria-label]');
            for (const el of ariaCandidates) {
                if (!isClickable(el)) continue;
                const label = normalize(el.getAttribute('aria-label'));
                if (label === 'chat' || label === 'next' || label.includes('start chat')) {
                    el.click();
                    log('Found action button via aria-label', 'success');
                    return true;
                }
            }

            if (dialog) {
                const candidates = Array.from(dialog.querySelectorAll('div[role="button"], a, button'));
                for (const el of candidates) {
                    if (!isClickable(el)) continue;
                    if (normalize(el.innerText || el.textContent) === 'chat') {
                        el.click();
                        log('Found Chat button via loose text match on clickable container', 'success');
                        return true;
                    }
                }
                
                const allElements = Array.from(dialog.querySelectorAll('div, span'));
                for (const el of allElements) {
                    if (normalize(el.innerText || el.textContent) === 'chat') {
                        const clickableParent = el.closest('div[role="button"], a, button') || el;
                        if (clickableParent && isClickable(clickableParent)) {
                            clickableParent.click();
                            log('Found Chat button via loose text match on inner element', 'success');
                            return true;
                        }
                        
                        if (el.childElementCount === 0 && isClickable(el)) {
                            el.click();
                            log('Found Chat button, clicked exact text element as fallback', 'success');
                            return true;
                        }
                    }
                }
            }
            return false;
        };

        for (let i = 0; i < 6; i++) {
            await wait(500);
            if (attemptClick()) {
                return true;
            }
        }

        const debugButtons = Array.from(document.querySelectorAll('button, div[role="button"], a[role="button"]'))
            .map(el => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim())
            .filter(Boolean)
            .slice(0, 15);
        log(`Chat button not found or remained disabled. Visible button labels: ${debugButtons.join(' | ') || 'none'}`, 'error');
        return false;
    }

    async function typeAndSendMessage(messageText) {
        log('Preparing DM composer...', 'search');

        const selectors = [
            'div[role="textbox"][contenteditable="true"][aria-placeholder]',
            'div[role="textbox"][contenteditable="true"]',
            'div[contenteditable="true"][data-lexical-editor="true"]',
            'div[data-lexical-editor="true"][role="textbox"]'
        ];

        let composer = null;
        for (let attempt = 0; attempt < 8 && !composer; attempt++) {
            for (const selector of selectors) {
                composer = document.querySelector(selector);
                if (composer) break;
            }
            if (!composer) await wait(350);
        }

        if (!composer) {
            log('Message composer not found', 'error');
            return false;
        }

        const getComposerText = () => (composer.innerText || composer.textContent || '').replace(/\s+/g, ' ').trim();
        const isSent = () => getComposerText().length === 0;
        const isMessageRequestSent = () => hasMessageRequestSentNotice(document);

        const placeCaretAtEnd = (el) => {
            const selection = window.getSelection();
            if (!selection) return;
            const range = document.createRange();
            range.selectNodeContents(el);
            range.collapse(false);
            selection.removeAllRanges();
            selection.addRange(range);
        };

        const triggerEnter = (target) => {
            if (!target) return;
            const eventInit = {
                key: 'Enter',
                code: 'Enter',
                keyCode: 13,
                which: 13,
                bubbles: true,
                cancelable: true
            };
            target.dispatchEvent(new KeyboardEvent('keydown', eventInit));
            target.dispatchEvent(new KeyboardEvent('keypress', eventInit));
            target.dispatchEvent(new KeyboardEvent('keyup', eventInit));
        };

        composer.focus();
        placeCaretAtEnd(composer);
        await wait(150);

        let typedWithExecCommand = false;
        try {
            document.execCommand('selectAll', false, null);
            document.execCommand('delete', false, null);
            composer.focus();
            placeCaretAtEnd(composer);
            typedWithExecCommand = document.execCommand('insertText', false, messageText);
        } catch (err) {
            typedWithExecCommand = false;
        }

        if (!typedWithExecCommand) {
            composer.focus();
            placeCaretAtEnd(composer);
            composer.dispatchEvent(new InputEvent('beforeinput', {
                data: messageText,
                inputType: 'insertText',
                bubbles: true,
                cancelable: true
            }));
            composer.textContent = messageText;
            composer.dispatchEvent(new InputEvent('input', {
                data: messageText,
                inputType: 'insertText',
                bubbles: true
            }));
        }

        composer.dispatchEvent(new Event('change', { bubbles: true }));
        await wait(200);

        log(`Typed message: ${messageText}`, 'success');

        composer.focus();
        triggerEnter(composer);
        triggerEnter(document.activeElement);
        triggerEnter(document);

        await wait(CONFIG.MESSAGE_SEND_DELAY);

        if (isSent()) {
            log('Sent message using Enter key', 'success');
            return true;
        }

        if (isMessageRequestSent()) {
            log('Message request sent. Continuing to next user.', 'success');
            return true;
        }

        const composerRoot = composer.closest('[data-pagelet="IGDComposerForCannes"], div[role="dialog"], main') || document;
        const clickable = composerRoot.querySelectorAll('button, div[role="button"], a[role="button"]');
        for (const el of clickable) {
            const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const label = (el.getAttribute('aria-label') || '').trim().toLowerCase();
            const title = (el.getAttribute('title') || '').trim().toLowerCase();
            const isDisabled = el.getAttribute('aria-disabled') === 'true' || ('disabled' in el && el.disabled);
            if (!isDisabled && (
                text === 'send' ||
                label === 'send' ||
                label.includes('send') ||
                title.includes('send') ||
                label.includes('press enter') ||
                !!el.querySelector('svg[aria-label*="Send"], svg[aria-label*="send"]')
            )) {
                el.click();
                await wait(CONFIG.MESSAGE_SEND_DELAY);
                if (isSent()) {
                    log('Sent message via Send button fallback', 'success');
                    return true;
                }
                if (isMessageRequestSent()) {
                    log('Message request sent via Send button fallback.', 'success');
                    return true;
                }
            }
        }

        const form = composer.closest('form');
        if (form) {
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
            await wait(CONFIG.MESSAGE_SEND_DELAY);
            if (isSent()) {
                log('Sent message via form submit fallback', 'success');
                return true;
            }
            if (isMessageRequestSent()) {
                log('Message request sent via form submit fallback.', 'success');
                return true;
            }
        }

        log(`Message send could not be confirmed. Composer text: "${getComposerText()}"`, 'error');
        return false;
    }

    // ==================== MAIN FLOW ====================
    async function startScraperFlow() {
        if (STATE.isScraping) {
            log('Scraping already in progress. Please wait.', 'warning');
            return;
        }

        STATE.dmStopRequested = false;
        STATE.currentDMIndex = -1;
        STATE.isNavigatingForDM = false;
        setScrapingState(true);

        try {
            const runConfig = await promptForRunConfig();
            if (!runConfig || !runConfig.modelName) {
                log('No username provided. Cancelled.', 'warning');
                setScrapingState(false);
                return;
            }

            saveRunConfig(runConfig);
            const senderName = runConfig.senderUsername || 'Unknown';
            await sendTelegramNotification(`${senderName}\nBot started for: @${runConfig.modelName}`, runConfig);
            await wait(1000); // Extra buffer to ensure request completion before navigation

            setScrapeOffset(0);
            sessionStorage.setItem('dmProcessedFollowers', JSON.stringify([]));

            sessionStorage.setItem('scraper_state', JSON.stringify({
                step: 'navigate_to_profile',
                modelName: runConfig.modelName
            }));

            STATE.modelName = runConfig.modelName;
            log(`Target: @${runConfig.modelName}`, 'info');
            log(`Batch size: ${runConfig.targetCount}`, 'info');

            await navigateToProfile(runConfig.modelName);
        } catch (error) {
            console.error('❌ Error during scraping:', error);
            restartFlowFromStart('startScraperFlow exception');
        }
    }

    async function checkAndContinueFlow() {
        const savedState = sessionStorage.getItem('scraper_state');
        if (!savedState) return;

        const state = JSON.parse(savedState);
        setScrapingState(true);
        STATE.modelName = state.modelName || null;
        log(`Resuming flow from step: ${state.step}`);

        if (state.step === 'navigate_to_profile') {
            const currentUrl = window.location.href;
            const expectedProfile = `instagram.com/${state.modelName}`;

            if (currentUrl.includes(expectedProfile)) {
                log(`On profile page for @${state.modelName}`, 'success');

                sessionStorage.setItem('scraper_state', JSON.stringify({
                    step: 'click_followers',
                    modelName: state.modelName
                }));

                await wait(2000);
                await dismissPopups();
                await wait(1000);

                const followersClicked = await clickFollowersLinkSimple();
                if (!followersClicked) {
                    log('Could not open followers modal. Check console for debug info.', 'error');
                    restartFlowFromStart('followers modal open failed');
                    return;
                }

                sessionStorage.setItem('scraper_state', JSON.stringify({
                    step: 'scrape_followers',
                    modelName: state.modelName
                }));

                await continueScraping(state.modelName);
            } else {
                log('Not on expected profile page', 'warning');
                restartFlowFromStart('unexpected profile URL');
            }
        }
    }

    async function continueScraping(modelName) {
        try {
            if (!STATE.isScraping) {
                log('Scraping was stopped before follower extraction', 'warning');
                return;
            }

            const runConfig = getCurrentRunConfig();
            if (!runConfig) {
                log('Run configuration missing. Please start again.', 'error');
                restartFlowFromStart('missing run configuration during scraping');
                return;
            }

            const startOffset = getScrapeOffset();

            const { result, uniqueUsernames, finalUsernames } = await scrapeFollowersFromModal(runConfig.targetCount, startOffset);

            if (!STATE.isScraping) {
                log('Scraping stopped before completion', 'warning');
                return;
            }

            console.log('\n' + '='.repeat(55));
            console.log('✨ SCRAPING COMPLETE ✨');
            console.log('='.repeat(55));
            console.log(`📊 Statistics:`);
            console.log(`   - Total elements found: ${finalUsernames.length}`);
            console.log(`   - Unique usernames: ${uniqueUsernames.length}`);
            console.log(`   - Returning batch: ${result.length} usernames (offset ${startOffset})`);

            if (result.length > 0) {
                console.log('\n📋 First 20 usernames:\n');
                result.slice(0, 20).forEach((username, index) => {
                    console.log(`${(index + 1).toString().padStart(2)}. @${username}`);
                });

                console.log('\n📋 Full JSON array:\n');
                console.log(JSON.stringify(result, null, 2));
            }

            try {
                await navigator.clipboard.writeText(JSON.stringify(result, null, 2));
                console.log('\n✅ Array copied to clipboard!');
            } catch (err) {
                console.log('\n⚠️ Could not copy to clipboard');
            }

            sessionStorage.setItem('scrapedFollowers', JSON.stringify(result));
            sessionStorage.setItem('scrapedFollowersFirst', result[0] || '');
            sessionStorage.setItem('dmProcessedFollowers', JSON.stringify([]));
            sessionStorage.removeItem('scraper_state');

            if (result.length > 0) {
                // Set up DM queue
                sessionStorage.setItem('auto_dm_queue', '1');
                STATE.currentDMIndex = 0;
                STATE.isNavigatingForDM = true;

                log('Auto-navigating to Direct Messages for DM processing...', 'info');
                window.location.href = 'https://www.instagram.com/direct/inbox/';
            } else {
                log('No followers scraped. Staying on current page.', 'info');
                setScrapingState(false);
            }

        } catch (error) {
            console.error('❌ Error during scraping:', error);
            restartFlowFromStart('continueScraping exception');
        }
    }

    async function processDMForUser(username) {
        log(`Auto-processing @${username}`, 'info');

        const previousThreadId = getActiveThreadIdFromUrl();

        if (!await clickNewMessageButton()) {
            log('Failed to open new message dialog', 'error');
            return { status: 'failed' };
        }

        if (!await searchForUser(username)) {
            log('Failed to search for user', 'error');
            return { status: 'failed' };
        }

        const searchResultStatus = await selectSearchResultForUser(username);
        if (searchResultStatus === 'no_account') {
            return { status: 'skipped_no_account' };
        }

        if (searchResultStatus === 'no_results') {
            return { status: 'skipped_no_results' };
        }

        if (!searchResultStatus) {
            log('Failed to select search result', 'error');
            return { status: 'failed' };
        }

        if (!await clickChatButton()) {
            log('Failed to click Chat button', 'error');
            return { status: 'failed' };
        }

        if (!await waitForConversationOpen(username, previousThreadId, 9000)) {
            log(`Conversation for @${username} did not open. Skipping send to avoid wrong recipient.`, 'error');
            return { status: 'skipped_conversation_not_open' };
        }

        const composerRoot = document.querySelector('[data-pagelet="IGDComposerForCannes"]') || document;
        if (hasMessageRequestSentNotice(composerRoot)) {
            log(`Message request already sent for @${username}. Skipping user.`, 'warning');
            return { status: 'skipped_message_request' };
        }

        log('Chat opened successfully!', 'success');

        const runConfig = getCurrentRunConfig();
        const templates = (runConfig && runConfig.messageTemplates) ? runConfig.messageTemplates : [...CONFIG.DEFAULT_MESSAGE_TEMPLATES];
        const chosenMessage = templates.length
            ? templates[Math.floor(Math.random() * templates.length)]
            : 'Hey!';

        const sent = await typeAndSendMessage(chosenMessage);
        return { status: sent ? 'sent' : 'failed' };
    }

    async function startNextBatchScrape() {
        const runConfig = getCurrentRunConfig();
        if (!runConfig || !runConfig.modelName) {
            log('Cannot continue to next batch: run configuration missing.', 'error');
            restartFlowFromStart('missing run config for next batch');
            return;
        }

        const currentBatch = (() => {
            try {
                const parsed = JSON.parse(sessionStorage.getItem('scrapedFollowers') || '[]');
                return Array.isArray(parsed) ? parsed : [];
            } catch (err) {
                return [];
            }
        })();

        const nextOffset = getScrapeOffset() + currentBatch.length;
        setScrapeOffset(nextOffset);
        sessionStorage.setItem('scraper_state', JSON.stringify({
            step: 'navigate_to_profile',
            modelName: runConfig.modelName
        }));
        sessionStorage.removeItem('auto_dm_queue');
        sessionStorage.setItem('dmProcessedFollowers', JSON.stringify([]));

        STATE.isNavigatingForDM = true;
        log(`Batch complete. Starting next scrape batch from offset ${nextOffset}.`, 'success');
        navigateToProfile(runConfig.modelName);
    }

    async function handleDMPage() {
        log('Direct Messages page detected - starting auto DM queue', 'info');

        const pendingFollowers = getPendingFollowers();
        if (pendingFollowers.length === 0) {
            if (shouldAutoRunDMQueue()) {
                await startNextBatchScrape();
            } else {
                log('No pending scraped followers found for DM queue.', 'warning');
                STATE.currentDMIndex = -1;
                STATE.isNavigatingForDM = false;
                setScrapingState(false);
            }
            return;
        }

        STATE.dmStopRequested = false;
        setScrapingState(true);

        const username = pendingFollowers[0];
        const processedCount = getProcessedFollowers().length;
        const totalInBatch = pendingFollowers.length + processedCount;

        log(`Processing user ${processedCount + 1}/${totalInBatch}: @${username}`, 'stats');

        // Process this single user
        const dmResult = await processDMForUser(username);

        if (dmResult.status === 'sent') {
            saveProcessedFollower(username);
            const totalSentThisRun = incrementSentCountThisRun();
            const successCount = getProcessedFollowers().length;
            log(`✅ Message sent to @${username} (${successCount}/${totalInBatch})`, 'success');

            const runConfig = getCurrentRunConfig();
            const notifyEvery = Math.max(1, parseInt(String(CONFIG.TELEGRAM_NOTIFY_EVERY_DMS || 10), 10) || 10);
            const milestone = Math.floor(totalSentThisRun / notifyEvery);
            const lastMilestone = getLastNotifiedMilestone();
            if (milestone > lastMilestone) {
                const milestoneSentCount = milestone * notifyEvery;
                const senderName = (runConfig && runConfig.senderUsername) || 'Unknown';
                const targetModel = (runConfig && runConfig.modelName) || 'Unknown';
                sendTelegramNotification(`${senderName}\nTarget: @${targetModel}\nDM sent : ${milestoneSentCount} messages`, runConfig);
                setLastNotifiedMilestone(milestone);
            }

            // Cooldown logic: after every COOLDOWN_THRESHOLD DMs, wait for COOLDOWN_DURATION_MS
            if (totalSentThisRun > 0 && totalSentThisRun % CONFIG.COOLDOWN_THRESHOLD === 0) {
                const cooldownMins = Math.floor(CONFIG.COOLDOWN_DURATION_MS / 60000);
                const senderName = (runConfig && runConfig.senderUsername) || 'Unknown';
                const targetModel = (runConfig && runConfig.modelName) || 'Unknown';
                
                log(`Cooldown triggered: ${CONFIG.COOLDOWN_THRESHOLD} DMs sent. Waiting ${cooldownMins} minutes...`, 'warning');
                await sendTelegramNotification(`${senderName}\nTarget: @${targetModel}\nCooldown started: ${CONFIG.COOLDOWN_THRESHOLD} DMs reached. Waiting ${cooldownMins} mins.`, runConfig);
                
                // Sleep in 1-minute increments to remain responsive to stop requests
                for (let i = 0; i < cooldownMins; i++) {
                    if (STATE.dmStopRequested || !STATE.isScraping) break;
                    log(`Cooldown in progress... ${cooldownMins - i} minutes remaining.`, 'info');
                    await wait(60000);
                }
                
                if (!STATE.dmStopRequested && STATE.isScraping) {
                    log('Cooldown finished. Resuming...', 'success');
                    await sendTelegramNotification(`${senderName}\nTarget: @${targetModel}\nCooldown finished. Resuming DMs...`, runConfig);
                }
            }
        } else if (dmResult.status === 'skipped_no_account') {
            saveProcessedFollower(username);
            log(`⏭️ Skipped @${username} because no account was found`, 'warning');
        } else if (dmResult.status === 'skipped_no_results') {
            saveProcessedFollower(username);
            log(`⏭️ Skipped @${username} because search returned no results`, 'warning');
        } else if (dmResult.status === 'skipped_message_request') {
            saveProcessedFollower(username);
            log(`⏭️ Skipped @${username} because a message request was already sent`, 'warning');
        } else if (dmResult.status === 'skipped_conversation_not_open') {
            saveProcessedFollower(username);
            log(`⏭️ Skipped @${username} because conversation did not open`, 'warning');
        } else {
            log(`❌ Failed to send message to @${username}`, 'error');
            saveProcessedFollower(username);
            restartFlowFromStart(`DM send failed for @${username}`);
            return;
        }

        // Check if there are more users to process
        const remaining = getPendingFollowers();
        if (remaining.length === 0 || STATE.dmStopRequested || !STATE.isScraping) {
            if (remaining.length === 0 && !STATE.dmStopRequested && STATE.isScraping) {
                await startNextBatchScrape();
            } else {
                sessionStorage.removeItem('auto_dm_queue');
                STATE.currentDMIndex = -1;
                STATE.isNavigatingForDM = false;
                log('DM queue completed or stopped.', 'stats');
                setScrapingState(false);
            }
            return;
        }

        // Navigate back to inbox for next user
        log(`Navigating back to inbox for next user. ${remaining.length} users remaining.`, 'info');
        STATE.currentDMIndex = getProcessedFollowers().length;
        STATE.isNavigatingForDM = true;

        // Wait a bit before navigating to ensure message is sent
        await wait(CONFIG.DM_BETWEEN_USERS_DELAY_MS);

        navigateToInbox();
    }

    function maybeHandleDMPage() {
        if (!window.location.href.includes('instagram.com/direct/inbox')) {
            return;
        }

        if (!shouldAutoRunDMQueue()) {
            log('DM inbox detected. Queue is idle until auto_dm_queue is enabled.', 'info');
            return;
        }

        if (STATE.isScraping && !STATE.isNavigatingForDM) {
            log('DM queue already running.', 'warning');
            return;
        }

        // Reset navigation flag
        STATE.isNavigatingForDM = false;

        // Auto-start DM processing
        setTimeout(() => {
            if (shouldAutoRunDMQueue()) {
                handleDMPage();
            }
        }, 2000);
    }

    // ==================== INITIALIZATION ====================
    function init() {
        const currentUrl = window.location.href.toLowerCase();
        if (currentUrl.includes('/accounts/suspended')) {
            const config = getCurrentRunConfig();
            const username = config ? config.senderUsername : 'Unknown';
            log('🚨 ACCOUNT SUSPENDED DETECTED!', 'error');
            sendTelegramNotification(`🚨 *ACCOUNT SUSPENDED*\n👤 Account: \`${username}\`\n🔗 URL: ${window.location.href}`, config, true);
            
            sessionStorage.removeItem('scraper_state');
            sessionStorage.removeItem('auto_dm_queue');
            setScrapingState(false);
            return;
        }
        const savedState = sessionStorage.getItem('scraper_state');
        setScrapingState(!!savedState);
        addScraperButton();

        // One-time migration for older versions that tracked processed followers in session only.
        const legacyProcessed = getProcessedFollowers();
        for (const username of legacyProcessed) {
            saveAllProcessedFollower(username);
        }

        const runConfig = getCurrentRunConfig();
        if (runConfig && !STATE.modelName) {
            STATE.modelName = runConfig.modelName;
        }

        // Restore DM queue state if applicable
        if (shouldAutoRunDMQueue()) {
            STATE.currentDMIndex = getSavedDMQueueIndex();
        }

        if (savedState) {
            setTimeout(checkAndContinueFlow, 2000);
        }

        maybeHandleDMPage();

        // Global error recovery: never let runtime errors permanently stop the flow.
        window.addEventListener('error', () => {
            if (STATE.isScraping) {
                restartFlowFromStart('window error event');
            }
        });

        window.addEventListener('unhandledrejection', () => {
            if (STATE.isScraping) {
                restartFlowFromStart('unhandled promise rejection');
            }
        });

        // Check for daily summary report due
        if (runConfig && runConfig.senderUsername) {
            processDailySummary(runConfig.senderUsername, 0);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Handle SPA navigation
    let lastUrl = location.href;
    new MutationObserver(() => {
        const url = location.href;
        if (url !== lastUrl) {
            lastUrl = url;
            setTimeout(() => {
                addScraperButton();

                const savedState = sessionStorage.getItem('scraper_state');
                if (savedState) {
                    checkAndContinueFlow();
                }
            }, 1000);

            if (url.includes('instagram.com/direct/inbox')) {
                maybeHandleDMPage();
            }
        }
    }).observe(document, {subtree: true, childList: true});

    console.log('🚀 Instagram Auto Scraper Script Loaded!');
    console.log('📌 Click the "Start Scraper" button to begin.');
    console.log('⚡ Auto mode: No approval needed - fully automated flow!');
    console.log('🔄 Navigation reset: Returns to inbox after each message send');
})();