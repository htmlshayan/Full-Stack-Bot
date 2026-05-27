(function() {
    'use strict';

    const log = (msg) => {
        console.log('🤖 [Comment Liker] | ' + msg);
        localStorage.setItem('ig_liker_status', msg);
    };
    const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));

    const CONFIG = {
        modelUsername: window.BOT_CONFIG?.target_model || '',
        postsCount: window.BOT_CONFIG?.posts_count || 5,
        likesPerPost: window.BOT_CONFIG?.likes_per_post || 10
    };

    if (!CONFIG.modelUsername) {
        log('ERROR: No target model provided in BOT_CONFIG');
        localStorage.setItem('ig_liker_status', 'ERROR');
        localStorage.setItem('ig_liker_error', 'No target model provided');
        return;
    }

    async function run() {
        localStorage.setItem('ig_liker_active', 'true');
        
        const stateStr = sessionStorage.getItem('liker_state');
        const state = stateStr ? JSON.parse(stateStr) : { step: 'navigate_profile' };

        log(`Current step: ${state.step}`);

        if (state.step === 'navigate_profile') {
            await wait(2000);
            const currentPath = location.pathname.replace(/\//g,'').toLowerCase();
            const targetPath = CONFIG.modelUsername.replace(/\//g,'').toLowerCase();
            
            if (currentPath !== targetPath) {
                log('Redirecting to profile: ' + CONFIG.modelUsername);
                window.location.href = `https://www.instagram.com/${CONFIG.modelUsername}/`;
                return;
            }

            log('Scanning for posts...');
            window.scrollBy(0, 1000);
            await wait(3000);

            let posts = Array.from(document.querySelectorAll('a[href*="/p/"]'))
                .map(a => a.href)
                .filter(href => !href.includes('/reels/'));
            posts = [...new Set(posts)];

            if (posts.length === 0) {
                log('No posts found. Retrying scan...');
                window.scrollBy(0, 1000);
                await wait(3000);
                posts = Array.from(document.querySelectorAll('a[href*="/p/"]')).map(a => a.href);
                posts = [...new Set(posts)];
            }

            posts = posts.slice(0, CONFIG.postsCount);

            if (posts.length > 0) {
                log(`Found ${posts.length} posts. Starting process.`);
                sessionStorage.setItem('liker_posts_queue', JSON.stringify(posts));
                sessionStorage.setItem('liker_state', JSON.stringify({ step: 'process_posts' }));
                localStorage.setItem('ig_liker_progress', `0/${posts.length} posts`);

                const nextPost = posts.shift();
                sessionStorage.setItem('liker_posts_queue', JSON.stringify(posts));
                window.location.href = nextPost;
            } else {
                log('ERROR: No posts found on profile');
                localStorage.setItem('ig_liker_status', 'ERROR');
                localStorage.setItem('ig_liker_error', 'No posts found on profile');
                sessionStorage.removeItem('liker_state');
            }
        }
        else if (state.step === 'process_posts') {
            await wait(4000); // Wait for post to load

            const queueStr = sessionStorage.getItem('liker_posts_queue');
            const queue = queueStr ? JSON.parse(queueStr) : [];
            const currentPostIndex = CONFIG.postsCount - queue.length;
            
            localStorage.setItem('ig_liker_progress', `${currentPostIndex}/${CONFIG.postsCount} posts`);
            log(`Processing post ${currentPostIndex}/${CONFIG.postsCount}`);

            const likedOnThisPost = await processCurrentPostComments(CONFIG.likesPerPost);
            
            let totalLiked = parseInt(localStorage.getItem('ig_liker_total_liked') || '0');
            localStorage.setItem('ig_liker_total_liked', totalLiked + likedOnThisPost);

            if (queue.length > 0) {
                const nextPost = queue.shift();
                sessionStorage.setItem('liker_posts_queue', JSON.stringify(queue));
                log(`Moving to next post...`);
                await wait(2000);
                window.location.href = nextPost;
            } else {
                log('COMPLETED');
                localStorage.setItem('ig_liker_status', 'COMPLETED');
                sessionStorage.removeItem('liker_state');
                sessionStorage.removeItem('liker_posts_queue');
            }
        }
    }

    async function processCurrentPostComments(maxLikes) {
        async function loadMore() {
            log('Loading comments...');
            let limit = 5; 
            while(limit > 0) {
                const loadBtn = Array.from(document.querySelectorAll('svg[aria-label="Load more comments"], svg[aria-label="View replies"]'))
                    .map(svg => svg.closest('button, [role="button"]'))
                    .filter(Boolean)[0];

                if (!loadBtn) {
                    const textBtn = Array.from(document.querySelectorAll('button, [role="button"]'))
                        .find(el => el.textContent && (el.textContent.includes('View all') || el.textContent.includes('View more')) && el.textContent.includes('comments'));
                    if (textBtn) {
                        textBtn.click();
                        await wait(2500);
                        limit--;
                        continue;
                    }
                    break;
                }

                loadBtn.click();
                await wait(2500);
                limit--;
            }
        }

        function findUnliked() {
            const buttons = [];
            // Target specific "Like" icons within comment sections
            document.querySelectorAll('svg[aria-label="Like"]').forEach(svg => {
                const btn = svg.closest('button, [role="button"]');
                if (btn) {
                    // Filter out post like button (usually larger and in a specific footer)
                    const isCommentLike = btn.closest('ul') || btn.closest('article div div div');
                    if (isCommentLike && btn.offsetWidth < 30) { 
                        buttons.push(btn);
                    }
                }
            });
            return buttons;
        }

        await loadMore();

        let toLike = findUnliked();
        log(`Found ${toLike.length} potential comments to like.`);

        let likedCount = 0;
        for (let btn of toLike) {
            if (likedCount >= maxLikes) break;
            
            // Check if already liked (aria-label might change or color)
            if (btn.querySelector('svg[aria-label="Unlike"]')) continue;

            try {
                btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                await wait(800);
                btn.click();
                likedCount++;
                log(`Liked comment ${likedCount}/${maxLikes} on this post`);
                await wait(1000 + Math.random() * 1500);
            } catch(e) {
                log('Click failed, skipping one');
            }
        }
        return likedCount;
    }

    // Initialize metrics if first run
    if (!localStorage.getItem('ig_liker_total_liked')) {
        localStorage.setItem('ig_liker_total_liked', '0');
    }

    run();

})();
