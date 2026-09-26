/* boot.js — compile the app once, then never again.
 *
 * THE PROBLEM THIS SOLVES
 * The whole UI is one ~610KB inline JSX block that Babel standalone used to
 * transpile in the browser on every single page load. Measured on this app:
 *
 *     domInteractive        408 ms
 *     domContentLoaded    2,868 ms
 *
 * Nearly two and a half seconds of that gap was Babel, and it was paid again on
 * every navigation to every tab — the API calls behind the screens were only
 * ever 60-150 ms, so the app felt slow for reasons that had nothing to do with
 * the data. Loading babel.min.js itself is a further cost: 2.8 MB to fetch,
 * parse and evaluate before the transpile can even start.
 *
 * THE FIX
 * Transpile once, keep the output, and on later loads inject the compiled
 * JavaScript directly — Babel is then never downloaded at all. The source is
 * hashed, so editing index.html invalidates the cache by itself and there is no
 * version number for anyone to forget to bump.
 *
 * Normally this is a build step. There is no Node on the machines this runs on,
 * so the browser does the build the first time and remembers the result.
 *
 * WHY IT IS SAFE TO SERVE CACHED CODE
 * The key is a hash of the exact source text plus the transform options. Any
 * edit to the app, and any change to how it is compiled, produces a different
 * key and a fresh compile. And the injected code has to prove it ran: the
 * source sets a sentinel as its last statement, so a cache entry that is
 * truncated, corrupt or half-written is detected, discarded, and recompiled
 * from source. A bad cache costs one slow load, never a broken page.
 */
(function () {
    'use strict';

    var SOURCE_ID = 'app-source';
    var STORE_KEY = 'kredo-app-bundle';
    var SENTINEL = '__KREDO_BOOTED__';

    /* Bump ONLY when the transform options below change. The source itself does
     * not need it — the source hash covers that. */
    var COMPILER_REV = 'r1';

    /* react only, deliberately not env.
     *
     * babel-standalone's default for a text/babel script also runs preset-env,
     * which down-compiles optional chaining, nullish coalescing, async/await and
     * the rest for browsers that have supported all of it for years. That is the
     * expensive half of the transpile and it buys nothing here: this app already
     * ships React 18 and Chart.js 4, neither of which runs on a browser old
     * enough to need it. JSX is the only thing that genuinely has to be
     * transformed. */
    var BABEL_OPTS = { presets: ['react'], sourceType: 'script', compact: false };

    var BABEL_SRC = '/static/babel.min.js';

    // ---------------------------------------------------------------------
    // cyrb53 — a fast, well-distributed 53-bit string hash.
    //
    // Not a checksum for security, just for identity: the only question is
    // whether this source is the same source we compiled last time. Over 610KB
    // it runs in a few milliseconds, against the ~2,500 ms it saves.
    // ---------------------------------------------------------------------
    function hash(str) {
        var h1 = 0xdeadbeef, h2 = 0x41c6ce57, ch;
        for (var i = 0; i < str.length; i++) {
            ch = str.charCodeAt(i);
            h1 = Math.imul(h1 ^ ch, 2654435761);
            h2 = Math.imul(h2 ^ ch, 1597334677);
        }
        h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
        h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
        return (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36);
    }

    // ---------------------------------------------------------------------
    // Storage. IndexedDB rather than localStorage: the compiled bundle is
    // around a megabyte, localStorage stores UTF-16 so it would take roughly
    // two of the five megabytes an origin gets, and a quota failure there is a
    // thrown exception mid-boot. IndexedDB has room and fails asynchronously.
    //
    // Every failure path here resolves rather than rejects. Caching is an
    // optimisation: if it does not work the app must still start, just slowly.
    // ---------------------------------------------------------------------
    function idb(mode, fn) {
        return new Promise(function (resolve) {
            var req;
            try { req = indexedDB.open('kredo-boot', 1); }
            catch (e) { return resolve(null); }

            req.onupgradeneeded = function () {
                var db = req.result;
                if (!db.objectStoreNames.contains('bundles')) db.createObjectStore('bundles');
            };
            req.onerror = function () { resolve(null); };
            req.onsuccess = function () {
                var db = req.result;
                var tx, store;
                try {
                    tx = db.transaction('bundles', mode);
                    store = tx.objectStore('bundles');
                } catch (e) { db.close(); return resolve(null); }
                fn(store, function (v) { resolve(v); });
                tx.oncomplete = function () { db.close(); };
                tx.onerror = function () { db.close(); resolve(null); };
                tx.onabort = function () { db.close(); resolve(null); };
            };
        });
    }

    function cacheGet(key) {
        return idb('readonly', function (store, done) {
            var r = store.get(STORE_KEY);
            r.onsuccess = function () {
                var v = r.result;
                done(v && v.key === key ? v.code : null);
            };
            r.onerror = function () { done(null); };
        });
    }

    function cachePut(key, code) {
        return idb('readwrite', function (store, done) {
            try { store.put({ key: key, code: code }, STORE_KEY); } catch (e) {}
            done(true);
        });
    }

    function cacheClear() {
        return idb('readwrite', function (store, done) {
            try { store.delete(STORE_KEY); } catch (e) {}
            done(true);
        });
    }

    // ---------------------------------------------------------------------
    function loadScript(src) {
        return new Promise(function (resolve, reject) {
            var s = document.createElement('script');
            s.src = src;
            s.onload = resolve;
            s.onerror = function () { reject(new Error('failed to load ' + src)); };
            document.head.appendChild(s);
        });
    }

    /* Injected as a real <script> element rather than eval'd.
     *
     * This is what Babel standalone does with a text/babel block, and it is the
     * reason the app's top-level `const`/`function` declarations keep behaving
     * the way they did: they land in script scope, visible to each other. An
     * indirect eval would scope them to the eval instead. Same code, same
     * semantics, no rewriting. */
    function run(code) {
        var s = document.createElement('script');
        s.textContent = code;
        document.body.appendChild(s);
        return window[SENTINEL] === true;
    }

    function fail(err) {
        console.error('[boot] could not start the application', err);
        var root = document.getElementById('root');
        if (root) {
            root.innerHTML =
                '<div style="font-family:system-ui,sans-serif;padding:2rem;max-width:40rem">'
              + '<h2 style="margin:0 0 .5rem">The application could not start.</h2>'
              + '<p style="color:#555;font-size:.9rem">' + String(err && err.message || err) + '</p>'
              + '<p style="color:#555;font-size:.9rem">Reloading the page will retry from source.</p>'
              + '</div>';
        }
    }

    // ---------------------------------------------------------------------
    (async function boot() {
        var el = document.getElementById(SOURCE_ID);
        if (!el) return fail(new Error('application source block not found'));

        var src = el.textContent;
        var key = COMPILER_REV + '-' + hash(src) + '-' + src.length;

        try {
            var cached = await cacheGet(key);
            if (cached && run(cached)) return;          // the fast path

            /* Either nothing cached, or what was cached did not set the
             * sentinel. Both mean: compile from source and replace it. */
            if (cached) await cacheClear();

            if (typeof window.Babel === 'undefined') await loadScript(BABEL_SRC);
            var code = window.Babel.transform(src, BABEL_OPTS).code;

            if (!run(code)) throw new Error('compiled bundle did not initialise');

            /* Stored only after the code has demonstrably run, so a bundle that
             * throws on the way up is never handed to the next page load. */
            cachePut(key, code);
        } catch (err) {
            fail(err);
        }
    })();
})();
