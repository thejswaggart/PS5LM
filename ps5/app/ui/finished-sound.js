// A soft two-note chime when the model has finished answering and waits for
// the user. scripts/build-app.sh has patches/0013 put this file inline in
// llama.cpp's web UI (index.html), so no Svelte build is needed.
//
// It watches the page's streamed POST v1/chat/completions requests: a stream
// that ends with finish_reason "stop" or "length" is an answer. One that ends
// with "tool_calls" is followed by the UI running the tool (POST tools) and
// asking again, unless it shows "Allow use of ...?" and waits for the user,
// which gets the chime too. The chime waits a moment and is dropped if another
// completion or tool call starts in that time (a tool loop, a title), so one
// wait gives one chime. A stopped generation ends without a finish_reason and
// gives none.
//
// Off for this browser: localStorage.setItem("ps5lm.finishedSound", "off")
//
// SPDX-License-Identifier: GPL-3.0-or-later
(function () {
    "use strict";
    if (typeof window.fetch !== "function" || window.__ps5lmFinishedSound) {
        return;
    }
    window.__ps5lmFinishedSound = true;

    var QUIET_MS = 800;      // no new completion for this long: the turn is over
    var APPROVAL_MS = 1500;  // a tool call the UI has not run by then waits for the user
    var active = 0;          // completion streams and tool calls under way
    var timer = 0;
    var audio = null;

    function enabled() {
        try {
            return window.localStorage.getItem("ps5lm.finishedSound") !== "off";
        } catch (e) {
            return true;
        }
    }

    // Browsers start audio only after a click or key press on the page; make the
    // context then, so the chime can play later with no gesture of its own.
    function unlock() {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) {
            return;
        }
        if (!audio) {
            try {
                audio = new Ctx();
            } catch (e) {
                return;
            }
        }
        if (audio.state === "suspended") {
            audio.resume();
        }
    }
    window.addEventListener("pointerdown", unlock, true);
    window.addEventListener("keydown", unlock, true);

    function note(freq, start, length, peak) {
        var osc = audio.createOscillator();
        var gain = audio.createGain();
        osc.type = "sine";
        osc.frequency.value = freq;
        gain.gain.setValueAtTime(0, start);
        gain.gain.linearRampToValueAtTime(peak, start + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + length);
        osc.connect(gain);
        gain.connect(audio.destination);
        osc.start(start);
        osc.stop(start + length + 0.05);
    }

    function chime() {
        if (!enabled() || !audio || audio.state !== "running") {
            return;
        }
        var t = audio.currentTime + 0.02;
        note(659.25, t, 0.55, 0.12);         // E5
        note(987.77, t + 0.13, 0.8, 0.10);   // B5, a fifth above
    }

    function finished(reason) {
        clearTimeout(timer);
        var wait = reason === "stop" || reason === "length" ? QUIET_MS
                 : reason === "tool_calls" ? APPROVAL_MS : 0;
        if (!wait) {
            return;
        }
        timer = setTimeout(function () {
            if (active === 0) {
                chime();
            }
        }, wait);
    }

    function isPost(input, init) {
        var method = (init && init.method) || (input && input.method) || "GET";
        return method.toUpperCase() === "POST";
    }

    function urlOf(input) {
        return typeof input === "string" ? input : (input && input.url) || "";
    }

    function isCompletion(input, init) {
        // not v1/chat/completions/control, and not the UI's non-streamed requests
        if (!isPost(input, init) || !/(^|\/)v1\/chat\/completions(\?|$)/.test(urlOf(input))) {
            return false;
        }
        var body = init && init.body;
        return typeof body === "string" && /"stream"\s*:\s*true/.test(body);
    }

    // The UI running a server tool: the turn goes on, whatever the tool takes.
    function isToolCall(input, init) {
        return isPost(input, init) && /(^|\/)tools(\?|$)/.test(urlOf(input));
    }

    async function watch(response) {
        var reason = null;
        var tail = "";
        var re = /"finish_reason"\s*:\s*"([a-z_]+)"/g;
        try {
            var reader = response.body.getReader();
            var decoder = new TextDecoder();
            for (;;) {
                var part = await reader.read();
                if (part.done) {
                    break;
                }
                // a finish_reason split across chunks still matches in tail + text
                var text = tail + decoder.decode(part.value, { stream: true });
                var m;
                re.lastIndex = 0;
                while ((m = re.exec(text)) !== null) {
                    reason = m[1];
                }
                tail = text.slice(-64);
            }
        } catch (e) {
            reason = null;  // aborted or failed: no chime
        }
        active--;
        finished(reason);
    }

    var originalFetch = window.fetch;
    window.fetch = function (input, init) {
        var promise = originalFetch.apply(this, arguments);
        var completion = false, tool = false;
        try {
            completion = isCompletion(input, init);
            tool = !completion && isToolCall(input, init);
        } catch (e) {
            completion = tool = false;
        }
        if (!completion && !tool) {
            return promise;
        }
        clearTimeout(timer);
        active++;
        if (tool) {
            // done when its answer has arrived; the next completion follows it
            return promise.then(function (response) {
                active--;
                return response;
            }, function (error) {
                active--;
                throw error;
            });
        }
        return promise.then(function (response) {
            if (response.ok && response.body) {
                // the UI reads the response itself; this reads a copy
                watch(response.clone());
            } else {
                active--;
            }
            return response;
        }, function (error) {
            active--;
            throw error;
        });
    };
})();
