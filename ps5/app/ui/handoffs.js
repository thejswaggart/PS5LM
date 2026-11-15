// A Handoffs button in the web UI: it lists the handoff notes the model saved
// with save_handoff (GET tools?list=handoffs, from patches/0011), newest first,
// without asking the model. Clicking a note puts "Load the handoff ..." in the
// message box, to send or change; nothing reaches the model until it is sent.
// scripts/build-app.sh has patches/0013 put this file inline in the web UI.
//
// SPDX-License-Identifier: GPL-3.0-or-later
(function () {
    "use strict";
    if (window.__ps5lmHandoffs) {
        return;
    }
    window.__ps5lmHandoffs = true;

    var css =
        "#ps5lm-handoffs-btn{position:fixed;top:10px;right:64px;z-index:60;padding:5px 11px;border-radius:8px;" +
        "font:500 13px system-ui,sans-serif;cursor:pointer;border:1px solid rgba(128,128,128,.35);" +
        "background:var(--background,#fff);color:var(--foreground,#111);opacity:.85}" +
        "#ps5lm-handoffs-btn:hover{opacity:1}" +
        "#ps5lm-handoffs{position:fixed;top:46px;right:16px;z-index:61;width:min(380px,calc(100vw - 32px));" +
        "max-height:min(460px,calc(100vh - 80px));overflow:auto;border-radius:10px;padding:6px;" +
        "border:1px solid rgba(128,128,128,.35);background:var(--popover,var(--background,#fff));" +
        "color:var(--popover-foreground,var(--foreground,#111));box-shadow:0 8px 28px rgba(0,0,0,.22);" +
        "font:14px system-ui,sans-serif}" +
        "#ps5lm-handoffs .h{display:flex;justify-content:space-between;align-items:center;padding:6px 8px 8px;" +
        "font-weight:600}" +
        "#ps5lm-handoffs .h button{border:0;background:none;color:inherit;cursor:pointer;font-size:18px;" +
        "line-height:1;opacity:.6}" +
        "#ps5lm-handoffs .n{display:block;width:100%;text-align:left;border:0;background:none;color:inherit;" +
        "cursor:pointer;padding:8px;border-radius:7px;font:inherit}" +
        "#ps5lm-handoffs .n:hover,#ps5lm-handoffs .n:focus{background:rgba(128,128,128,.16);outline:none}" +
        "#ps5lm-handoffs .m{display:block;font-size:12px;opacity:.65;margin-top:2px}" +
        "#ps5lm-handoffs .e{padding:8px;opacity:.7}";

    function el(tag, cls, text) {
        var e = document.createElement(tag);
        if (cls) {
            e.className = cls;
        }
        if (text !== undefined) {
            e.textContent = text;
        }
        return e;
    }

    function ago(seconds) {
        var s = Math.max(0, Date.now() / 1000 - seconds);
        if (s < 90) {
            return "just now";
        }
        if (s < 5400) {
            return Math.round(s / 60) + " min ago";
        }
        if (s < 129600) {
            return Math.round(s / 3600) + " h ago";
        }
        return new Date(seconds * 1000).toLocaleDateString();
    }

    function size(bytes) {
        return bytes < 1024 ? bytes + " B" : (bytes / 1024).toFixed(1) + " KB";
    }

    // The UI's message box, the last visible one on the page: a CodeMirror
    // editor (contenteditable), or a textarea in other versions of the UI.
    function messageBox() {
        var all = document.querySelectorAll(".cm-content[contenteditable=true], textarea");
        for (var i = all.length - 1; i >= 0; i--) {
            if (all[i].offsetParent !== null && !all[i].disabled) {
                return all[i];
            }
        }
        return null;
    }

    // Replace what is in it as typing would, so the UI's own state follows.
    function fill(text) {
        var box = messageBox();
        if (!box) {
            return false;
        }
        box.focus();
        if (box.tagName === "TEXTAREA") {
            var setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
            setter.call(box, text);
            box.dispatchEvent(new Event("input", { bubbles: true }));
            box.setSelectionRange(text.length, text.length);
            return true;
        }
        // CodeMirror's own view (cmTile in newer versions, cmView before): a
        // change made there is what typing makes. insertText below is the
        // fallback; it does nothing on the first click after the box lost focus.
        var tile = box.cmTile || box.cmView;
        var view = tile && (tile.view || (tile.rootView && tile.rootView.view));
        if (view && typeof view.dispatch === "function") {
            view.dispatch({
                changes: { from: 0, to: view.state.doc.length, insert: text },
                selection: { anchor: text.length }
            });
            view.focus();
            return true;
        }
        var range = document.createRange();
        range.selectNodeContents(box);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        return document.execCommand("insertText", false, text);
    }

    var panel = null;

    function close() {
        if (panel) {
            panel.remove();
            panel = null;
        }
    }

    function show(items, error) {
        close();
        panel = el("div");
        panel.id = "ps5lm-handoffs";
        var head = el("div", "h", "Handoff notes");
        var x = el("button", "", "×");
        x.title = "Close";
        x.onclick = close;
        head.appendChild(x);
        panel.appendChild(head);
        if (error) {
            panel.appendChild(el("div", "e", error));
        } else if (!items.length) {
            panel.appendChild(el("div", "e", "No handoff notes saved yet."));
        }
        (items || []).forEach(function (h) {
            var b = el("button", "n", h.name);
            b.appendChild(el("span", "m", ago(h.modified) + " · " + size(h.bytes)));
            b.title = "Put “Load the handoff " + h.name + "” in the message box";
            b.onclick = function () {
                if (fill("Load the handoff \"" + h.name + "\" and carry on with its work.")) {
                    close();
                }
            };
            panel.appendChild(b);
        });
        document.body.appendChild(panel);
    }

    function open() {
        if (panel) {
            close();
            return;
        }
        fetch("./tools?list=handoffs", { cache: "no-store" }).then(function (r) {
            if (!r.ok) {
                throw new Error(r.status === 403 ? "Server tools are off." : "The server said " + r.status + ".");
            }
            return r.json();
        }).then(function (items) {
            show(Array.isArray(items) ? items : [], null);
        }, function (e) {
            show([], "Could not list them: " + e.message);
        });
    }

    function init() {
        var style = el("style", "", css);
        document.head.appendChild(style);
        var btn = el("button", "", "Handoffs");
        btn.id = "ps5lm-handoffs-btn";
        btn.type = "button";
        btn.title = "List the saved handoff notes";
        btn.onclick = open;
        document.body.appendChild(btn);
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape") {
                close();
            }
        });
        document.addEventListener("pointerdown", function (e) {
            if (panel && !panel.contains(e.target) && e.target !== btn) {
                close();
            }
        }, true);
    }

    if (document.body) {
        init();
    } else {
        document.addEventListener("DOMContentLoaded", init);
    }
})();
