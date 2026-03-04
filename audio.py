# =============================================================
#  audio.py  —  Browser audio via Web Audio API
#
#  Tones are synthesised entirely in the browser using the Web
#  Audio API — no audio files, no extra Python packages.
#
#  How it works:
#    components.html() re-renders only when its HTML string
#    changes.  A unique timestamp comment is embedded in each
#    call so Streamlit sees new content, re-renders the iframe,
#    and the JavaScript executes.
#
#  Usage:
#    audio.queue("alarm_pump")   # call anywhere in the script
#    audio.render()              # call once, at end of script
# =============================================================

import time
import streamlit as st
import streamlit.components.v1 as components


# ------------------------------------------------------------------
#  Shared JS helpers injected into every sound iframe
# ------------------------------------------------------------------

_TONE_FN = """
function tone(ctx, freq, dur, type, vol, delay) {
    var osc  = ctx.createOscillator();
    var gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.type = type;
    osc.frequency.setValueAtTime(freq, ctx.currentTime + delay);
    gain.gain.setValueAtTime(0,   ctx.currentTime + delay);
    gain.gain.linearRampToValueAtTime(vol, ctx.currentTime + delay + 0.01);
    gain.gain.linearRampToValueAtTime(0,   ctx.currentTime + delay + dur - 0.02);
    osc.start(ctx.currentTime + delay);
    osc.stop( ctx.currentTime + delay + dur);
}
"""

# Distorted alternating siren: 10 bursts, ~2.5 seconds total.
# Reused by all critical alarms so they share the same unmistakable sound.
_SIREN_JS = """
    var dc = new Float32Array(512);
    for (var k = 0; k < 512; k++) {
        var x = k * 2 / 512 - 1;
        dc[k] = Math.sign(x) * Math.pow(Math.abs(x), 0.3);
    }
    var freqs = [1500, 700, 1500, 700, 1500, 700, 1500, 700, 1500, 700];
    for (var i = 0; i < freqs.length; i++) {
        var osc  = ctx.createOscillator();
        var dist = ctx.createWaveShaper();
        var g    = ctx.createGain();
        dist.curve = dc;
        osc.connect(dist); dist.connect(g); g.connect(ctx.destination);
        osc.type = 'square';
        osc.frequency.setValueAtTime(freqs[i], ctx.currentTime + i * 0.25);
        var lfo = ctx.createOscillator();
        var lg  = ctx.createGain();
        lfo.frequency.value = 10; lg.gain.value = 40;
        lfo.connect(lg); lg.connect(osc.frequency);
        lfo.start(ctx.currentTime + i * 0.25);
        lfo.stop( ctx.currentTime + i * 0.25 + 0.23);
        g.gain.setValueAtTime(0,   ctx.currentTime + i * 0.25);
        g.gain.linearRampToValueAtTime(1.0, ctx.currentTime + i * 0.25 + 0.01);
        g.gain.linearRampToValueAtTime(0,   ctx.currentTime + i * 0.25 + 0.23);
        osc.start(ctx.currentTime + i * 0.25);
        osc.stop( ctx.currentTime + i * 0.25 + 0.25);
    }
"""


def _siren_plus_voice(message):
    """Return JS: siren burst followed by a spoken voice message."""
    return _SIREN_JS + f"""
    setTimeout(function() {{
        if ('speechSynthesis' in window) {{
            window.speechSynthesis.cancel();
            var u = new SpeechSynthesisUtterance('{message}');
            u.rate = 0.85; u.pitch = 0.8; u.volume = 1.0;
            window.speechSynthesis.speak(u);
        }}
    }}, 2600);
"""


# ------------------------------------------------------------------
#  Sound definitions
#
#  alarm_*    : siren + specific voice message, repeats every refresh
#               while the condition is active.
#  mode_change: short attention tone (~1 s) + voice, fires once per
#               mode transition.
#  ack        : soft double-beep + voice confirmation.
# ------------------------------------------------------------------

_SOUNDS = {
    "alarm_pump": _siren_plus_voice(
        "Warning. Pump stall detected. Pump ticks have not changed."
    ),
    "alarm_threshold": _siren_plus_voice(
        "Warning. Sensor threshold exceeded. Check dashboard for details."
    ),
    "alarm_stale": _siren_plus_voice(
        "Warning. No new data received. The CSV file may have stopped updating."
    ),

    "mode_change": """
        tone(ctx, 1000, 0.12, 'sine', 0.7, 0.00);
        tone(ctx,  800, 0.12, 'sine', 0.7, 0.15);
        tone(ctx, 1000, 0.12, 'sine', 0.7, 0.30);
        setTimeout(function() {
            if ('speechSynthesis' in window) {
                window.speechSynthesis.cancel();
                var u = new SpeechSynthesisUtterance('Mode changed');
                u.rate = 0.95; u.pitch = 1.0; u.volume = 1.0;
                window.speechSynthesis.speak(u);
            }
        }, 600);
    """,

    "ack": """
        tone(ctx, 880, 0.06, 'sine', 0.4, 0.00);
        tone(ctx, 660, 0.10, 'sine', 0.3, 0.07);
        setTimeout(function() {
            if ('speechSynthesis' in window) {
                window.speechSynthesis.cancel();
                var u = new SpeechSynthesisUtterance('Alarms acknowledged');
                u.rate = 1.0; u.pitch = 1.0; u.volume = 0.9;
                window.speechSynthesis.speak(u);
            }
        }, 250);
    """,
}


# ------------------------------------------------------------------
#  Public API
# ------------------------------------------------------------------

def queue(name):
    """
    Add a sound to the play queue for this render cycle.
    Duplicates are ignored — each sound plays at most once per cycle.
    """
    if "snd_queue" not in st.session_state:
        st.session_state.snd_queue = []
    if name not in st.session_state.snd_queue:
        st.session_state.snd_queue.append(name)


def render():
    """
    Render all queued sounds as 1-pixel hidden iframes and clear the queue.
    Must be called exactly once, at the very end of the Streamlit script.
    """
    q = getattr(st.session_state, "snd_queue", [])
    if not q:
        return

    # Unique timestamp forces the iframe to re-render even if the
    # sound name is the same as last cycle.
    ts = time.time()

    for name in q:
        if name not in _SOUNDS:
            continue
        html = (
            f'<!DOCTYPE html><html><body style="margin:0;padding:0;overflow:hidden">\n'
            f'<script>\n'
            f'// t={ts:.6f} snd={name}\n'
            f'(function() {{\n'
            f'    try {{\n'
            f'        var Ctx = window.AudioContext || window.webkitAudioContext;\n'
            f'        if (!Ctx) return;\n'
            f'        var ctx = new Ctx();\n'
            f'        if (ctx.state === "suspended") ctx.resume();\n'
            f'        {_TONE_FN}\n'
            f'        {_SOUNDS[name]}\n'
            f'    }} catch(e) {{ console.error("audio error", e); }}\n'
            f'}})();\n'
            f'</script></body></html>'
        )
        components.html(html, height=1)

    st.session_state.snd_queue = []
