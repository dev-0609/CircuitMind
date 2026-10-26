import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

# ---------------------------------------------------------------------
# CircuitMind V12 — unified diagnosis and repair workflow for supported circuits
# ---------------------------------------------------------------------
DEFAULT_NGSPICE = r"E:\ngspice\ngspice-47_64\Spice64\bin\ngspice_con.exe"
NGSPICE_EXE = os.environ.get("NGSPICE_EXE", DEFAULT_NGSPICE)

st.set_page_config(page_title="CircuitMind", page_icon="⚡", layout="wide")

st.title("⚡ CircuitMind")
st.caption("Natural-language circuit design • Multi-component SPICE simulation • Diagnosis • Verified repair")
st.markdown("---")


def find_ngspice():
    """Find ngspice at the configured path or on PATH."""
    configured = Path(NGSPICE_EXE)
    if configured.is_file():
        return str(configured)
    for command in ("ngspice_con.exe", "ngspice.exe", "ngspice"):
        found = shutil.which(command)
        if found:
            return found
    return None


def simulate_divider(vin, r1, r2):
    """Run an actual ngspice operating-point simulation."""
    executable = find_ngspice()
    if not executable:
        raise FileNotFoundError(
            "Could not find ngspice. Update DEFAULT_NGSPICE near the top of main.py "
            "or set NGSPICE_EXE to your ngspice executable."
        )

    netlist = f"""CircuitMind voltage divider
V1 in 0 DC {float(vin):.12g}
R1 in out {float(r1):.12g}
R2 out 0 {float(r2):.12g}
.control
op
print v(out)
quit
.endc
.end
"""
    with tempfile.TemporaryDirectory() as temp_dir:
        circuit_file = Path(temp_dir) / "divider.cir"
        circuit_file.write_text(netlist, encoding="ascii")
        result = subprocess.run(
            [executable, "-b", str(circuit_file)],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=temp_dir,
            errors="replace",
        )

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError("ngspice returned an error:\n" + output[-2500:])

    match = re.search(
        r"v\(out\)\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)",
        output,
        flags=re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"\b(?:v\(out\)|out)\s+([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)",
            output,
            flags=re.IGNORECASE,
        )
    if not match:
        raise RuntimeError(
            "ngspice ran, but CircuitMind could not parse v(out).\n\n"
            "ngspice output:\n" + output[-2500:]
        )
    return float(match.group(1)), output, netlist


def simulate_led_series(vin, r_series, vf, model_is_approx=True):
    """Run an ngspice DC operating-point simulation of a source, resistor and diode.

    A simple Shockley diode model is used as a generic LED approximation. Real LED
    behaviour depends on its datasheet; the simulated forward voltage is not forced
    to equal the user's nominal Vf exactly.
    """
    executable = find_ngspice()
    if not executable:
        raise FileNotFoundError(
            "Could not find ngspice. Update DEFAULT_NGSPICE or set NGSPICE_EXE."
        )
    # Use a generic diode model with modest saturation current and emission coefficient.
    # The nominal Vf input is used to estimate a plausible saturation current at 10 mA.
    thermal_voltage = 0.02585
    emission = 2.0
    target_current = 0.01
    saturation_current = target_current / (pow(2.718281828, max(vf, 0.05) / (emission * thermal_voltage)) - 1.0)
    saturation_current = min(max(saturation_current, 1e-18), 1e-6)
    netlist = f"""CircuitMind LED series circuit
V1 in 0 DC {float(vin):.12g}
R1 in led_anode {float(r_series):.12g}
D1 led_anode 0 LEDMODEL
.model LEDMODEL D(Is={saturation_current:.12g} N={emission:.6g} Rs=2)
.control
op
print v(led_anode)
print i(V1)
quit
.endc
.end
"""
    with tempfile.TemporaryDirectory() as temp_dir:
        circuit_file = Path(temp_dir) / "led_series.cir"
        circuit_file.write_text(netlist, encoding="ascii")
        result = subprocess.run(
            [executable, "-b", str(circuit_file)],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=temp_dir,
            errors="replace",
        )
    output = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError("ngspice returned an error:\n" + output[-2500:])

    def parse_value(pattern, label):
        match = re.search(pattern, output, flags=re.IGNORECASE)
        if not match:
            raise RuntimeError(f"ngspice ran, but CircuitMind could not parse {label}.\n" + output[-2500:])
        return float(match.group(1))

    v_led = parse_value(
        r"v\(led_anode\)\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)",
        "LED voltage",
    )
    current = parse_value(
        r"i\(v1\)\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)",
        "source current",
    )
    return v_led, abs(current), output, netlist


def apply_led_resistor_recommendation(value):
    """Apply a suggested LED resistor and mark the next simulation as a repair check."""
    st.session_state["led_r"] = float(value)
    st.session_state["led_repair_pending"] = {
        "resistor": float(value),
        "target_ma": float(st.session_state.get("led_target_ma", 10.0)),
    }


def diagnose_led_result(result, target_ma=10.0):
    """Rule-based resistor power guidance for a simulated LED circuit.

    Target-current PASS/FAIL is rendered separately in the main UI to avoid
    showing duplicate diagnosis messages.
    """
    messages = []
    if result["resistor_power"] > 0.25:
        messages.append(("warning", f"Resistor dissipation is {result['resistor_power'] * 1000:.1f} mW, above a 0.25 W rating. Use a suitably rated resistor."))
    elif result["resistor_power"] > 0.125:
        messages.append(("warning", f"Resistor dissipation is {result['resistor_power'] * 1000:.1f} mW. Check derating and choose a suitable power rating."))
    else:
        messages.append(("info", f"Resistor dissipation is {result['resistor_power'] * 1000:.1f} mW; verify the actual component's rated power and derating."))
    return messages


def simulate_led_circuit(vin, r_series, vf):
    """Validate and simulate a simple LED + series-resistor circuit."""
    if vin <= 0 or r_series <= 0 or vf <= 0:
        raise ValueError("Supply voltage, resistor, and nominal LED forward voltage must be positive.")
    if vin <= vf:
        raise ValueError("Supply voltage must be greater than nominal LED forward voltage.")
    v_led, current, log, netlist = simulate_led_series(vin, r_series, vf)
    return {
        "vin": vin,
        "r_series": r_series,
        "vf_nominal": vf,
        "v_led": v_led,
        "current_a": current,
        "resistor_drop": vin - v_led,
        "resistor_power": (vin - v_led) ** 2 / r_series,
        "log": log,
        "netlist": netlist,
    }


def make_led_schematic(vin, r_series, vf_nominal, v_led=None, current_a=None, glow_color="#ff3b30", wire_color="#7dd3fc", flow_speed=2.8, glow_strength=7):
    """Draw a simple source-resistor-LED schematic with simulated readings when available."""
    led_v_text = f"{v_led:.3f} V" if v_led is not None else "Not simulated"
    current_text = f"{current_a * 1000:.2f} mA" if current_a is not None else "Not simulated"
    simulated_on = current_a is not None and current_a > 0.0001
    led_glow = "url(#ledGlow)" if simulated_on else "none"
    # Animation remains illustrative; its speed is scaled by simulated current.
    speed = max(0.7, min(6.0, flow_speed * (10.0 / max(current_a * 1000.0, 0.5)))) if simulated_on else flow_speed
    flow_markers = (
        f'<g fill="{glow_color}" opacity="0.95">'
        f'<circle r="5"><animateMotion dur="{speed:.2f}s" repeatCount="indefinite" path="M150 80 H285 H345 H465 H575 V245 H150 V80"/></circle>'
        f'<circle r="5"><animateMotion begin="-{speed*0.33:.2f}s" dur="{speed:.2f}s" repeatCount="indefinite" path="M150 80 H285 H345 H465 H575 V245 H150 V80"/></circle>'
        f'<circle r="5"><animateMotion begin="-{speed*0.66:.2f}s" dur="{speed:.2f}s" repeatCount="indefinite" path="M150 80 H285 H345 H465 H575 V245 H150 V80"/></circle>'
        '</g>'
    ) if simulated_on else ""
    svg = f"""
    <div style="width:100%;overflow-x:auto;border:1px solid #263449;border-radius:14px;background:#0b1220;padding:12px">
    <svg viewBox="0 0 720 330" role="img" aria-label="LED series circuit schematic" style="display:block;width:100%;min-width:320px;max-height:360px">
      <defs>
        <pattern id="ledgrid" width="24" height="24" patternUnits="userSpaceOnUse">
          <path d="M24 0H0V24" fill="none" stroke="#1b2940" stroke-width="1"/>
        </pattern>
        <filter id="ledGlow" x="-80%" y="-80%" width="260%" height="260%">
          <feGaussianBlur stdDeviation="{glow_strength}" result="blur"/>
          <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>
      <rect x="0" y="0" width="720" height="330" rx="10" fill="url(#ledgrid)"/>
      <g stroke="{wire_color}" stroke-width="4" fill="none" stroke-linecap="round" stroke-linejoin="round">
        <path d="M150 80 H285"/>
        <path d="M345 80 H465"/>
        <path d="M515 80 H575 V245 H150 V205"/>
        <path d="M150 145 V80"/>
      </g>
      <g stroke="#e2e8f0" stroke-width="3" fill="#111c2e">
        <circle cx="150" cy="175" r="30"/>
        <rect x="285" y="61" width="60" height="38" rx="3"/>
        <path d="M465 55 L465 105 L510 80 Z" fill="{glow_color}" style="filter:{led_glow}"/>
        <path d="M515 54 V106" fill="none" stroke="{glow_color}" style="filter:{led_glow}"/>
      </g>
      <g stroke="{glow_color}" stroke-width="2.5" fill="none" stroke-linecap="round">
        <path d="M493 48 L507 34 M499 60 L513 46"/>
        <path d="M503 34 L507 34 L507 38 M513 46 L513 46 L513 50"/>
      </g>
      {flow_markers}
      <g fill="#e2e8f0" font-family="Segoe UI,Arial,sans-serif" font-size="16">
        <text x="42" y="35" font-size="18" font-weight="700">LED SERIES CIRCUIT</text>
        <text x="150" y="169" text-anchor="middle" font-size="18">+</text>
        <text x="150" y="188" text-anchor="middle" font-size="18">−</text>
        <text x="315" y="48" text-anchor="middle" font-weight="700">R1</text>
        <text x="315" y="123" text-anchor="middle" fill="#94a3b8">{resistor_label(r_series)}</text>
        <text x="488" y="133" text-anchor="middle" font-weight="700">LED (D1)</text>
        <text x="488" y="154" text-anchor="middle" fill="#94a3b8">Nominal Vf {vf_nominal:.2f} V</text>
        <text x="62" y="235" fill="#94a3b8">Supply = {vin:.3g} V</text>
        <text x="570" y="180" text-anchor="end" fill="#a7f3d0">VLED: {led_v_text}</text>
        <text x="570" y="204" text-anchor="end" fill="#a7f3d0">I: {current_text}</text>
        <text x="360" y="284" text-anchor="middle" fill="#94a3b8">GND · 0 V</text>
        <text x="24" y="315" fill="#64748b" font-size="12">SCHEMATIC · generic diode model · not to scale</text>
        <text x="680" y="315" text-anchor="end" fill="#fbbf24" font-size="12">{'ANIMATED CURRENT' if simulated_on else 'RUN SIMULATION TO ACTIVATE'}</text>
      </g>
      <g stroke="#e2e8f0" stroke-width="3" stroke-linecap="round">
        <path d="M555 245 H595 M563 253 H587 M571 261 H579"/>
      </g>
    </svg>
    </div>
    """
    return svg


def resistor_label(value):
    """Format resistance in readable engineering units."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.3g} MΩ"
    if value >= 1000:
        return f"{value / 1000:.3g} kΩ"
    return f"{value:.3g} Ω"


def e12_values():
    """Return supported E12 resistor values from 1 Ω to 1 MΩ."""
    bases = [10, 12, 15, 18, 22, 27, 33, 39, 47, 56, 68, 82]
    values = []
    for decade in range(-1, 7):
        scale = 10 ** decade
        values.extend(base * scale for base in bases)
    return sorted(v for v in values if 1 <= v <= 1_000_000)


def nearest_e12(value):
    """Return the closest E12 resistor value in the supported range."""
    if value <= 0:
        raise ValueError("Resistance must be positive.")
    return min(e12_values(), key=lambda v: abs(v - value))


def e12_at_least(value):
    """Choose the smallest supported E12 value not below the calculated resistance.

    For a target LED current treated as a maximum, this avoids selecting a
    standard resistor that would nominally push current above the target.
    """
    if value <= 0:
        raise ValueError("Resistance must be positive.")
    candidates = [v for v in e12_values() if v >= value]
    return min(candidates) if candidates else 1_000_000


def voltage_divider_r2(vin, target, r1):
    """Calculate R2 for Vout = Vin * R2 / (R1 + R2)."""
    if vin <= 0 or target <= 0:
        raise ValueError("Input and target voltages must be positive.")
    if target >= vin:
        raise ValueError("A passive voltage divider needs a target below the input voltage.")
    if r1 <= 0:
        raise ValueError("R1 must be positive.")
    return target * r1 / (vin - target)


def make_schematic(vin, r1, r2, vout=None, faulted=False):
    """Create an SVG circuit schematic; values are labelled from app state."""
    r1_text = resistor_label(r1)
    r2_text = resistor_label(r2)
    vout_text = f"{vout:.3f} V" if vout is not None else "Not simulated"
    fault_color = "#ef4444" if faulted else "#38bdf8"
    r2_color = "#ef4444" if faulted else "#38bdf8"

    svg = f"""
    <div style="width:100%;overflow-x:auto;border:1px solid #263449;border-radius:14px;background:#0b1220;padding:12px">
    <svg viewBox="0 0 720 390" role="img" aria-label="Voltage divider schematic with source, two resistors, output node and ground" style="display:block;width:100%;min-width:320px;max-height:420px">
      <defs>
        <pattern id="cmgrid" width="24" height="24" patternUnits="userSpaceOnUse">
          <path d="M24 0H0V24" fill="none" stroke="#1b2940" stroke-width="1"/>
        </pattern>
      </defs>
      <rect x="0" y="0" width="720" height="390" rx="10" fill="url(#cmgrid)"/>
      <g stroke="#7dd3fc" stroke-width="4" fill="none" stroke-linecap="round" stroke-linejoin="round">
        <path d="M170 78 H360 V118"/>
        <path d="M360 166 V204"/>
        <path d="M360 254 V300 H170 V247"/>
        <path d="M170 203 V78"/>
        <path d="M360 204 H520"/>
      </g>
      <g stroke="#e2e8f0" stroke-width="3" fill="#111c2e">
        <circle cx="170" cy="225" r="22"/>
        <rect x="344" y="118" width="32" height="48" rx="3"/>
        <rect x="344" y="204" width="32" height="50" rx="3" stroke="{r2_color}" fill="#111c2e"/>
      </g>
      <g fill="#e2e8f0" font-family="Segoe UI,Arial,sans-serif" font-size="16">
        <text x="48" y="45" font-size="18" font-weight="700">DC SOURCE</text>
        <text x="170" y="219" text-anchor="middle" font-size="18">+</text>
        <text x="170" y="237" text-anchor="middle" font-size="18">−</text>
        <text x="395" y="137" font-weight="700">R1</text>
        <text x="395" y="158" fill="#94a3b8">{r1_text}</text>
        <text x="395" y="222" font-weight="700" fill="{fault_color}">R2{"  •  FAULT" if faulted else ""}</text>
        <text x="395" y="243" fill="#94a3b8">{r2_text}</text>
        <text x="535" y="194" font-weight="700" fill="#a7f3d0">VOUT</text>
        <text x="535" y="218" fill="#a7f3d0">{vout_text}</text>
        <text x="65" y="278" fill="#94a3b8">Vin = {vin:.3g} V</text>
        <text x="360" y="340" text-anchor="middle" fill="#94a3b8">GND · 0 V</text>
        <text x="28" y="370" fill="#64748b" font-size="12">SCHEMATIC · voltage-divider topology · not to scale</text>
      </g>
      <g stroke="#e2e8f0" stroke-width="3" stroke-linecap="round">
        <path d="M340 300 H380 M347 308 H373 M354 316 H366"/>
      </g>
      <circle cx="360" cy="204" r="6" fill="#a7f3d0"/>
      <circle cx="520" cy="204" r="5" fill="#a7f3d0"/>
    </svg>
    </div>
    """
    return svg


# ----------------------- Natural-language designer ----------------------
st.markdown("## 🧠 Natural-language Circuit Designer")
st.write(
    "Describe a voltage divider or LED + series-resistor circuit in plain English. "
    "This prototype uses rule-based parsing and circuit equations, then ngspice verifies simulations."
)
design_prompt = st.text_input(
    "Describe your circuit",
    placeholder="Example: I need to convert 5V to 2.5V using a voltage divider",
    key="design_prompt",
)
with st.expander("Examples you can try"):
    st.markdown(
        "- `Convert 5V to 2.5V using a voltage divider`\n"
        "- `I need 12 V input and 3 V output`\n"
        "- `5V input, target output 1.8V, R1 1000 ohms`\n"
        "- `12 volt input to 3.3 volt output, R1 2.2 kohms`\n"
        "- `Build an LED circuit with a 5V supply, red LED, and target current 10mA`\n"
        "- `5V supply, LED forward voltage 2V, target current 15mA`"
    )

def parse_led_request(prompt):
    """Extract supply voltage, nominal LED forward voltage and target current."""
    text = prompt.lower().replace(",", " ")
    voltage_matches = re.findall(
        r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:v\b|volt(?:s)?\b)",
        text,
        flags=re.IGNORECASE,
    )
    if not voltage_matches:
        raise ValueError("For an LED design, include a supply voltage, e.g. '5V supply'.")
    supply = float(voltage_matches[0])

    # Explicit LED forward voltage is preferred. Otherwise infer a rough nominal value
    # from common color words, while clearly treating it as an assumption.
    vf_match = re.search(
        r"(?:forward voltage|led voltage|vf)\s*(?:of|=|:)?\s*(\d+(?:\.\d+)?)\s*(?:v\b|volt(?:s)?\b)?",
        text,
        flags=re.IGNORECASE,
    )
    assumed_vf = False
    if vf_match:
        vf = float(vf_match.group(1))
    else:
        color_defaults = [
            (("red",), 2.0),
            (("yellow",), 2.1),
            (("green",), 2.2),
            (("blue", "white"), 3.0),
        ]
        vf = None
        for words, value in color_defaults:
            if any(word in text for word in words):
                vf = value
                assumed_vf = True
                break
        if vf is None:
            vf = 2.0
            assumed_vf = True

    current_match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:ma|milliamp(?:s|ere)?s?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if current_match:
        target_ma = float(current_match.group(1))
    else:
        amp_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:a|amp(?:s|ere)?s?)\b", text, flags=re.IGNORECASE)
        if amp_match:
            target_ma = float(amp_match.group(1)) * 1000
        else:
            target_ma = 10.0

    if supply <= 0 or vf <= 0 or target_ma <= 0:
        raise ValueError("Supply voltage, LED forward voltage, and target current must be positive.")
    if supply <= vf:
        raise ValueError("Supply voltage must be greater than the LED forward voltage.")
    if target_ma > 100:
        raise ValueError("This prototype limits suggested LED current to 100 mA. Check your LED datasheet.")
    exact_r = (supply - vf) / (target_ma / 1000.0)
    if not 1 <= exact_r <= 1_000_000:
        raise ValueError("Calculated resistor is outside the supported range (1 Ω to 1 MΩ).")
    standard_r = nearest_e12(exact_r)
    predicted_current_ma = (supply - vf) / standard_r * 1000
    return {
        "vin": supply,
        "vf": vf,
        "target_ma": target_ma,
        "exact_r": exact_r,
        "standard_r": standard_r,
        "predicted_current_ma": predicted_current_ma,
        "assumed_vf": assumed_vf,
    }


def parse_voltage_request(prompt):
    """Extract input/output voltage and optional R1 from a simple text request."""
    text = prompt.lower().replace(",", " ")
    # Voltage tokens such as 5V, 5 V, 2.5 volts.
    matches = re.findall(
        r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:v\b|volt(?:s)?\b)",
        text,
        flags=re.IGNORECASE,
    )
    if len(matches) < 2:
        raise ValueError(
            "Please include both an input voltage and a desired output voltage, "
            "for example: 'Convert 5V to 2.5V using a voltage divider'."
        )
    vin_value = float(matches[0])
    target_value = float(matches[1])
    if vin_value <= 0 or target_value <= 0:
        raise ValueError("Input and target voltages must both be greater than zero.")
    if target_value >= vin_value:
        raise ValueError(
            "For a passive voltage divider, the target output must be lower than the input voltage."
        )
    r1_matches = re.search(
        r"\br1\b\s*(?:=|:|of)?\s*(\d+(?:\.\d+)?)\s*(k\s*ohms?|kohms?|kΩ|ohms?|Ω)?",
        text,
        flags=re.IGNORECASE,
    )
    if r1_matches:
        r1_value = float(r1_matches.group(1))
        unit = (r1_matches.group(2) or "").replace(" ", "").lower()
        if unit.startswith("k"):
            r1_value *= 1000
    else:
        r1_value = 1000.0
    if not 1 <= r1_value <= 1_000_000:
        raise ValueError("R1 must be between 1 Ω and 1 MΩ in this prototype.")
    r2_value = voltage_divider_r2(vin_value, target_value, r1_value)
    if not 1 <= r2_value <= 1_000_000:
        raise ValueError(
            f"The calculated R2 is {r2_value:.3g} Ω, outside the supported 1 Ω–1 MΩ range. "
            "Try a different R1 value."
        )
    return vin_value, r1_value, r2_value, target_value

design_col, design_button_col = st.columns([4, 1])
with design_button_col:
    st.write("")
    st.write("")
    generate_design = st.button("✨ Generate design", type="primary", width="stretch")

if generate_design:
    try:
        prompt_lower = design_prompt.lower()
        if "led" in prompt_lower or "diode" in prompt_lower:
            led_design = parse_led_request(design_prompt)
            st.session_state["circuit_type"] = "LED + series resistor"
            st.session_state["led_vin"] = led_design["vin"]
            st.session_state["led_vf"] = led_design["vf"]
            st.session_state["led_r"] = float(led_design["standard_r"])
            st.session_state["generated_led_design"] = {
                **led_design,
                "prompt": design_prompt,
            }
            st.success("LED design generated. Review the values below, then run ngspice to verify it.")
        else:
            design_vin, design_r1, design_r2, design_target = parse_voltage_request(design_prompt)
            st.session_state["circuit_type"] = "Voltage divider"
            st.session_state["vin_input"] = design_vin
            st.session_state["r1_input"] = design_r1
            st.session_state["r2_input"] = design_r2
            st.session_state["target_vout"] = design_target
            st.session_state["generated_design"] = {
                "vin": design_vin,
                "r1": design_r1,
                "r2": design_r2,
                "target": design_target,
                "prompt": design_prompt,
            }
            st.success("Voltage-divider design generated. Review the component values below, then run ngspice to verify it.")
        st.rerun()
    except ValueError as error:
        st.error(str(error))

if "generated_led_design" in st.session_state:
    led_design = st.session_state["generated_led_design"]
    if led_design["prompt"] == design_prompt:
        st.info(
            f"LED design proposal: supply = {led_design['vin']:g} V, nominal Vf = {led_design['vf']:g} V, "
            f"target current = {led_design['target_ma']:g} mA. Exact resistor = {led_design['exact_r']:.2f} Ω; "
            f"nearest E12 value = {led_design['standard_r']:g} Ω (idealized estimate ≈ "
            f"{led_design['predicted_current_ma']:.2f} mA before diode-model effects)."
        )
        if led_design["assumed_vf"]:
            st.caption("No explicit LED forward voltage was provided, so a rough typical value was assumed from the LED color or defaulted to 2.0 V. Confirm the datasheet.")
        st.caption("This is a calculated proposal. Run the LED ngspice simulation below to see the generic diode model's result.")

if "generated_design" in st.session_state:
    design = st.session_state["generated_design"]
    standard_r2 = nearest_e12(design["r2"])
    estimated_vout = design["vin"] * standard_r2 / (design["r1"] + standard_r2)
    st.info(
        f"Calculated R2 = {design['r2']:.3f} Ω. Nearest supported E12 option: "
        f"{standard_r2:g} Ω, which predicts Vout ≈ {estimated_vout:.4f} V. "
        "This is a calculated proposal; it becomes simulation-verified only after you run ngspice."
    )
    if abs(estimated_vout - design["target"]) > max(0.02, abs(design["target"]) * 0.02):
        st.warning(
            "The nearest E12 value may miss your target tolerance. The app keeps the exact calculated "
            "R2 in the circuit inputs so you can compare exact and practical values."
        )

st.markdown("---")

# ----------------------- Unified Circuit Doctor ---------------------------
st.markdown("## 🩺 Unified Circuit Doctor")
st.write(
    "CircuitMind currently supports two tested circuit templates: a voltage divider and an LED with a series resistor. "
    "Each template has its own simulation and diagnosis rules. Results are only considered verified after ngspice runs."
)
with st.expander("How diagnosis and repair work"):
    st.markdown(
        "1. **Design:** enter component values or describe the requirement in natural language.\n"
        "2. **Simulate:** run ngspice to establish a baseline.\n"
        "3. **Inject a controlled fault:** change a supported component value.\n"
        "4. **Diagnose:** compare simulated measurements and explain the likely effect.\n"
        "5. **Repair:** apply a calculated or standard-value recommendation.\n"
        "6. **Verify:** rerun ngspice; do not treat a recommendation as verified until the simulation succeeds."
    )

# -------------------------- Circuit type selector ------------------------
st.markdown("## 🔌 Choose circuit type")
circuit_type = st.radio(
    "Supported circuit",
    options=["Voltage divider", "LED + series resistor"],
    horizontal=True,
    key="circuit_type",
    help="These are the two circuit topologies implemented in this prototype.",
)

if circuit_type == "LED + series resistor":
    st.write("Configure and simulate a simple LED circuit. The LED is represented by a generic diode model.")
    led_col1, led_col2, led_col3 = st.columns(3)
    with led_col1:
        led_vin = st.number_input("Supply voltage (V)", min_value=0.2, max_value=100.0, value=5.0, step=0.5, key="led_vin")
    with led_col2:
        led_vf = st.number_input("Nominal LED forward voltage (V)", min_value=0.2, max_value=5.0, value=2.0, step=0.1, key="led_vf")
    with led_col3:
        led_r = st.number_input("Series resistor (Ω)", min_value=1.0, max_value=1_000_000.0, value=300.0, step=10.0, key="led_r")
    led_target_ma = st.number_input(
        "Target LED current (mA)", min_value=0.1, max_value=100.0,
        value=10.0, step=1.0, key="led_target_ma"
    )
    led_sim_clicked = st.button("▶ Simulate LED circuit with ngspice", type="primary", width="stretch")
    if led_sim_clicked:
        try:
            with st.spinner("Running ngspice LED operating-point simulation..."):
                led_result = simulate_led_circuit(led_vin, led_r, led_vf)
            led_result["target_ma_at_run"] = led_target_ma
            st.session_state["led_result"] = led_result
            pending_repair = st.session_state.get("led_repair_pending")
            if pending_repair and abs(led_r - pending_repair["resistor"]) < 1e-9:
                measured_ma = led_result["current_a"] * 1000.0
                passed = pending_repair["target_ma"] * 0.90 <= measured_ma <= pending_repair["target_ma"] * 1.10
                st.session_state["led_repair_verification"] = {
                    "resistor": led_r,
                    "target_ma": pending_repair["target_ma"],
                    "measured_ma": measured_ma,
                    "passed": passed,
                }
                st.session_state.pop("led_repair_pending", None)
            st.success("LED circuit simulation completed.")
        except Exception as error:
            st.error(f"LED simulation failed: {error}")
    led_result = st.session_state.get("led_result")
    st.subheader("🧭 Fault Injection & Repair Workflow")
    st.caption(
        "Guided demo: baseline simulation → fault injection → diagnosis → repair → verification."
    )
    with st.expander("Show the five demo stages", expanded=True):
        st.markdown(
            "1. **Baseline:** run ngspice with the intended resistor.\n"
            "2. **Inject fault:** change the resistor to an intentionally incorrect value and rerun.\n"
            "3. **Diagnose:** compare simulated current with the selected target.\n"
            "4. **Repair:** apply the recommended E12 resistor.\n"
            "5. **Verify:** rerun ngspice and check whether current is within ±10% of target."
        )

    st.markdown("### 🔌 LED circuit schematic")
    with st.expander("🎨 Customize circuit glow & animation", expanded=False):
        glow_col, wire_col = st.columns(2)
        with glow_col:
            led_glow_color = st.color_picker("LED / current glow", "#ff3b30", key="led_glow_color")
        with wire_col:
            led_wire_color = st.color_picker("Wire colour", "#7dd3fc", key="led_wire_color")
        anim_col, strength_col = st.columns(2)
        with anim_col:
            led_flow_speed = st.slider("Animation speed", 0.7, 5.0, 2.8, 0.1, key="led_flow_speed", help="Lower duration values make the particles move faster.")
        with strength_col:
            led_glow_strength = st.slider("Glow intensity", 2, 14, 7, 1, key="led_glow_strength")
        st.caption("These are visual settings. Current readings and repair checks still come from ngspice.")
    result_is_current = (
        led_result is not None
        and (led_result["vin"], led_result["vf_nominal"], led_result["r_series"])
        == (led_vin, led_vf, led_r)
    )
    st.components.v1.html(
        make_led_schematic(
            led_vin,
            led_r,
            led_vf,
            led_result["v_led"] if result_is_current else None,
            led_result["current_a"] if result_is_current else None,
            glow_color=led_glow_color,
            wire_color=led_wire_color,
            flow_speed=led_flow_speed,
            glow_strength=led_glow_strength,
        ),
        height=360,
        scrolling=False,
    )
    if not result_is_current:
        st.caption("Run the LED simulation to show verified LED voltage and current on the schematic.")
    if led_result:
        same = (
            led_result["vin"], led_result["vf_nominal"], led_result["r_series"]
        ) == (led_vin, led_vf, led_r)
        target_same = led_result.get("target_ma_at_run", led_target_ma) == led_target_ma
        if not same or not target_same:
            st.warning("LED inputs or target current changed since the last run. Simulate again to refresh the diagnosis.")
            st.session_state.pop("led_repair_verification", None)
        lm1, lm2, lm3, lm4 = st.columns(4)
        lm1.metric("Simulated LED voltage", f'{led_result["v_led"]:.4f} V')
        lm2.metric("Simulated current", f'{led_result["current_a"] * 1000:.3f} mA')
        lm3.metric("Resistor power", f'{led_result["resistor_power"] * 1000:.3f} mW')
        lm4.metric("Resistor voltage drop", f'{led_result["resistor_drop"]:.4f} V')
        st.markdown("### 🩺 LED Circuit Doctor")
        if target_same:
            for level, message in diagnose_led_result(led_result, led_target_ma):
                if level == "success":
                    st.success(message)
                elif level == "warning":
                    st.warning(message)
                else:
                    st.info(message)

            measured_ma = led_result["current_a"] * 1000.0
            target_pass = led_target_ma * 0.90 <= measured_ma <= led_target_ma * 1.10
            if target_pass:
                st.success("✅ Target check: PASS — ngspice current is within ±10% of the selected target.")
            else:
                st.warning("⚠️ Target check: NOT PASS — apply a resistor recommendation and rerun ngspice.")

            repair_check = st.session_state.get("led_repair_verification")
            if repair_check and (
                repair_check["resistor"] == led_r
                and repair_check["target_ma"] == led_target_ma
                and result_is_current
            ):
                if repair_check["passed"]:
                    st.success(
                        f"🔧 REPAIR VERIFIED — {led_r:g} Ω produced {measured_ma:.2f} mA, "
                        f"within ±10% of the {led_target_ma:.2f} mA target."
                    )
                else:
                    st.error(
                        f"🔧 REPAIR NOT VERIFIED — {led_r:g} Ω produced {measured_ma:.2f} mA, "
                        f"outside ±10% of the {led_target_ma:.2f} mA target. Try another value."
                    )
            elif st.session_state.get("led_repair_pending"):
                st.info("Repair value applied. Run ngspice again to verify whether the repair meets the target.")

            if led_vin > led_vf:
                exact_recommended = (led_vin - led_vf) / (led_target_ma / 1000.0)
                if 1 <= exact_recommended <= 1_000_000:
                    practical_recommended = e12_at_least(exact_recommended)
                    idealized_current = (led_vin - led_vf) / practical_recommended * 1000
                    st.markdown(
                        f"**Resistor recommendation for a {led_target_ma:.2f} mA target:** "
                        f"{exact_recommended:.2f} Ω calculated; conservative E12 choice "
                        f"**{practical_recommended:g} Ω** (idealized estimate {idealized_current:.2f} mA)."
                    )
                    if idealized_current > led_target_ma * 1.001:
                        st.warning("Even the selected standard value estimates current above the target. Increase resistance or lower the target.")
                    else:
                        st.caption("Selection rule: choose the smallest E12 value at or above the calculated resistance, rather than the numerically closest value.")
                    st.button(
                        "🛠️ Apply recommended resistor",
                        key="apply_led_recommendation",
                        width="stretch",
                        on_click=apply_led_resistor_recommendation,
                        args=(practical_recommended,),
                    )
                    st.caption("After applying the value, click the ngspice simulation button again to verify the repair.")

            st.caption("Use the actual LED datasheet and check the resistor's rated power before building hardware.")
        led_chart = go.Figure()
        led_chart.add_bar(
            x=["Supply", "LED", "Resistor drop"],
            y=[led_result["vin"], led_result["v_led"], led_result["resistor_drop"]],
            text=[
                f'{led_result["vin"]:.3f} V',
                f'{led_result["v_led"]:.3f} V',
                f'{led_result["resistor_drop"]:.3f} V',
            ],
            textposition="auto",
        )
        led_chart.update_layout(
            title="LED circuit voltages from ngspice",
            yaxis_title="Voltage (V)",
            template="plotly_dark",
            height=330,
            margin=dict(l=20, r=20, t=55, b=20),
        )
        st.plotly_chart(led_chart, width="stretch")
        with st.expander("View generated LED SPICE netlist"):
            st.code(led_result["netlist"], language="text")
        with st.expander("View LED ngspice output"):
            st.code(led_result["log"], language="text")

        st.markdown("---")
        st.subheader("🧪 LED Fault Injection")
        st.caption(
            "For the demo, first record a baseline, inject one resistor fault, rerun the simulation, "
            "then apply a repair and rerun to verify it."
        )
        st.write("Temporarily change the series resistor and run a second ngspice simulation to see how the LED current changes.")
        fault_resistor = st.number_input(
            "Fault-injected series resistor (Ω)",
            min_value=1.0,
            max_value=1_000_000.0,
            value=float(max(1.0, led_r / 3.0)),
            step=10.0,
            key="led_fault_r",
        )
        if st.button("🧪 Simulate LED resistor fault", key="simulate_led_fault", width="stretch"):
            try:
                with st.spinner("Simulating fault-injected LED circuit..."):
                    fault_result = simulate_led_circuit(led_vin, fault_resistor, led_vf)
                st.session_state["led_fault_result"] = {
                    **fault_result,
                    "baseline_r": led_r,
                    "baseline_current": led_result["current_a"],
                    "baseline_power": led_result["resistor_power"],
                }
                st.rerun()
            except Exception as error:
                st.error(f"LED fault simulation failed: {error}")

        fault = st.session_state.get("led_fault_result")
        if fault and (
            fault["vin"], fault["vf_nominal"], fault["baseline_r"], fault_resistor
        ) == (led_vin, led_vf, led_r, fault_resistor) and result_is_current:
            current_delta_ma = (fault["current_a"] - fault["baseline_current"]) * 1000
            power_delta_mw = (fault["resistor_power"] - fault["baseline_power"]) * 1000
            f1, f2, f3 = st.columns(3)
            f1.metric("Baseline current", f'{fault["baseline_current"] * 1000:.3f} mA')
            f2.metric("Fault current", f'{fault["current_a"] * 1000:.3f} mA', f'{current_delta_ma:+.3f} mA')
            f3.metric("Fault resistor power", f'{fault["resistor_power"] * 1000:.3f} mW', f'{power_delta_mw:+.3f} mW')

            st.caption("Both baseline and fault values below come from successful ngspice operating-point runs.")
            if fault_resistor < led_r and fault["current_a"] > fault["baseline_current"] * 1.10:
                st.warning(
                    f"Diagnosis: reducing the series resistor from {led_r:g} Ω to {fault_resistor:g} Ω "
                    "increased simulated LED current. Check the LED's maximum continuous current rating."
                )
            elif fault_resistor > led_r and fault["current_a"] < fault["baseline_current"] * 0.90:
                st.info(
                    f"Diagnosis: increasing the series resistor from {led_r:g} Ω to {fault_resistor:g} Ω "
                    "reduced simulated LED current and may make the LED dimmer."
                )
            else:
                st.info("The simulated current change is small or does not match the simple expected direction; inspect the model and results.")
            with st.expander("View fault-injected SPICE netlist"):
                st.code(fault["netlist"], language="text")
            with st.expander("View fault-injected ngspice output"):
                st.code(fault["log"], language="text")

    st.info(
        "LED mode uses a generic diode model calibrated from the nominal forward-voltage input. "
        "For accurate hardware design, use the specific LED's datasheet/model and verify current limits."
    )
else:
    st.caption("Voltage-divider mode: use the natural-language designer below, or adjust the values directly.")

# ------------------------------- Inputs -------------------------------
input_col, schematic_col = st.columns([0.9, 1.1], gap="large")

with input_col:
    st.subheader("🔧 Circuit setup")
    vin = st.number_input(
        "Input voltage (V)", min_value=0.1, max_value=100.0,
        value=5.0, step=0.5, key="vin_input"
    )
    r1 = st.number_input(
        "Resistor R1 (Ω)", min_value=1.0, max_value=1_000_000.0,
        value=1000.0, step=100.0, key="r1_input"
    )
    r2 = st.number_input(
        "Resistor R2 (Ω)", min_value=1.0, max_value=1_000_000.0,
        value=1000.0, step=100.0, key="r2_input"
    )
    target_vout = st.number_input(
        "Desired output voltage (V)", min_value=0.0, max_value=100.0,
        value=2.5, step=0.1, key="target_vout"
    )

    run_clicked = st.button(
        "▶ Run SPICE Simulation", type="primary", width="stretch"
    )

with schematic_col:
    st.subheader("🔌 Live circuit schematic")
    last_result = st.session_state.get("simulation_result")
    current_inputs = (vin, r1, r2)
    result_matches_inputs = (
        last_result is not None
        and (last_result["vin"], last_result["r1"], last_result["r2"]) == current_inputs
    )
    displayed_vout = last_result["vout"] if result_matches_inputs else None
    st.components.v1.html(
        make_schematic(vin, r1, r2, displayed_vout),
        height=420,
        scrolling=False,
    )
    if not result_matches_inputs:
        st.caption("Run the simulation to populate the schematic with verified ngspice readings.")
    else:
        st.caption("Schematic readings are linked to the latest simulation for these exact input values.")

if run_clicked:
    try:
        with st.spinner("Running ngspice simulation..."):
            simulated_vout, simulation_log, netlist = simulate_divider(vin, r1, r2)
        st.session_state["simulation_result"] = {
            "vin": vin,
            "r1": r1,
            "r2": r2,
            "vout": simulated_vout,
            "log": simulation_log,
            "netlist": netlist,
        }
        st.success("Simulation completed successfully!")
        st.rerun()
    except Exception as error:
        st.error(f"Simulation failed: {error}")

# ------------------------------- Results -------------------------------
if "simulation_result" in st.session_state:
    original = st.session_state["simulation_result"]
    st.markdown("---")
    st.subheader("📊 Simulation results")

    inputs_match = (original["vin"], original["r1"], original["r2"]) == (vin, r1, r2)
    if not inputs_match:
        st.warning("Inputs changed since the last run. Run the simulation again to refresh verified readings.")

    m1, m2, m3 = st.columns(3)
    m1.metric("Simulated output", f'{original["vout"]:.6f} V')
    m2.metric("Input voltage", f'{original["vin"]:.3f} V')
    m3.metric("Voltage drop across R1", f'{original["vin"] - original["vout"]:.6f} V')

    chart = go.Figure()
    chart.add_bar(
        x=["Input voltage", "Simulated output"],
        y=[original["vin"], original["vout"]],
        text=[f'{original["vin"]:.3f} V', f'{original["vout"]:.6f} V'],
        textposition="auto",
    )
    chart.update_layout(
        title="ngspice operating-point results",
        yaxis_title="Voltage (V)",
        template="plotly_dark",
        height=330,
        margin=dict(l=20, r=20, t=55, b=20),
    )
    st.plotly_chart(chart, width="stretch")

    with st.expander("View generated SPICE netlist"):
        st.code(original["netlist"], language="text")
    with st.expander("View ngspice console output"):
        st.code(original["log"], language="text")

    # ---------------------------- Circuit Doctor -------------------------
    st.markdown("---")
    st.subheader("🩺 Circuit Doctor")
    st.write(
        "Compare the latest simulated output with your target. This first version uses "
        "voltage-divider equations and rule-based reasoning; it does not claim to be an AI model."
    )

    if not inputs_match:
        st.info("Run the simulation again before using Circuit Doctor so its advice is based on current inputs.")
    else:
        error_v = original["vout"] - target_vout
        tolerance = max(0.02, abs(target_vout) * 0.02)
        d1, d2, d3 = st.columns(3)
        d1.metric("Target output", f"{target_vout:.6f} V")
        d2.metric("Actual output", f'{original["vout"]:.6f} V')
        d3.metric("Target error", f"{error_v:+.6f} V")

        if abs(error_v) <= tolerance:
            st.success("Circuit is within the selected 2% (or 20 mV minimum) target tolerance.")
        elif target_vout >= original["vin"]:
            st.warning(
                "This passive voltage divider cannot produce an output above its input voltage. "
                "Choose a target below the input voltage or use a different circuit topology."
            )
        elif target_vout <= 0:
            st.warning("A zero-volt target is not achievable with finite positive R1 and R2 values in this simple divider.")
        else:
            # For a divider Vout = Vin * R2 / (R1 + R2), solve for R2.
            denominator = original["vin"] - target_vout
            recommended_r2 = (target_vout * original["r1"]) / denominator
            if recommended_r2 < 1 or recommended_r2 > 1_000_000:
                st.warning(
                    f"The calculated R2 is {recommended_r2:.3g} Ω, outside this app's supported "
                    "range (1 Ω to 1 MΩ). Adjust the target or R1."
                )
            else:
                st.warning("The simulated output is outside the target tolerance.")
                practical_r2 = nearest_e12(recommended_r2)
                practical_vout = original["vin"] * practical_r2 / (original["r1"] + practical_r2)
                st.markdown(
                    f"**Calculated adjustment:** R2 ≈ **{recommended_r2:.3f} Ω**. "
                    f"Nearest E12 resistor: **{practical_r2:g} Ω** (predicted output "
                    f"≈ {practical_vout:.4f} V). Vin and R1 are held constant."
                )
                repair_choice = st.radio(
                    "Which recommendation should we test?",
                    options=["Exact calculated value", "Nearest E12 standard value"],
                    horizontal=True,
                    key="repair_choice",
                )
                selected_r2 = recommended_r2 if repair_choice == "Exact calculated value" else float(practical_r2)
                if st.button("🛠️ Apply recommendation and verify", type="primary", width="stretch"):
                    try:
                        with st.spinner("Applying suggested R2 and verifying with ngspice..."):
                            repaired_vout, repaired_log, repaired_netlist = simulate_divider(
                                original["vin"], original["r1"], selected_r2
                            )
                        st.session_state["repair_result"] = {
                            "vin": original["vin"],
                            "r1": original["r1"],
                            "r2": selected_r2,
                            "target": target_vout,
                            "vout": repaired_vout,
                            "log": repaired_log,
                            "netlist": repaired_netlist,
                        }
                        st.rerun()
                    except Exception as error:
                        st.error(f"Repair verification failed: {error}")

        repair = st.session_state.get("repair_result")
        if repair and (
            repair["vin"], repair["r1"], repair["target"]
        ) == (original["vin"], original["r1"], target_vout):
            st.markdown("### 🔁 Repair verification")
            repair_error = repair["vout"] - repair["target"]
            r1m, r2m, r3m = st.columns(3)
            r1m.metric("Recommended R2", f'{repair["r2"]:.3f} Ω')
            r2m.metric("Verified ngspice output", f'{repair["vout"]:.6f} V')
            r3m.metric("Remaining target error", f"{repair_error:+.6f} V")
            repair_tolerance = max(0.02, abs(repair["target"]) * 0.02)
            if abs(repair_error) <= repair_tolerance:
                st.success("Repair verified: ngspice confirms the output is within target tolerance.")
            else:
                st.warning("The proposed adjustment was simulated, but the output remains outside target tolerance.")
            with st.expander("View repair verification netlist"):
                st.code(repair["netlist"], language="text")
            with st.expander("View repair verification log"):
                st.code(repair["log"], language="text")

    # -------------------------- Fault injection -------------------------
    st.markdown("---")
    st.subheader("🛠️ Fault injection & diagnosis")
    st.write("Change R2 to model a component-value fault, then simulate the modified circuit.")

    fault_r2 = st.number_input(
        "Modified R2 (Ω)",
        min_value=1.0,
        max_value=1_000_000.0,
        value=float(original["r2"] * 2),
        step=100.0,
        key="fault_r2",
    )

    fault_preview_col, fault_action_col = st.columns([1.2, 1])
    with fault_preview_col:
        st.markdown("**Modified circuit preview**")
        st.components.v1.html(
            make_schematic(original["vin"], original["r1"], fault_r2, None, faulted=True),
            height=420,
            scrolling=False,
        )
    with fault_action_col:
        st.markdown("**What changes?**")
        st.write(f"R2: {resistor_label(original['r2'])} → {resistor_label(fault_r2)}")
        st.write("Run a second ngspice simulation to compare the modified circuit.")
        run_fault = st.button("🧪 Simulate modified circuit", width="stretch")

    if run_fault:
        try:
            with st.spinner("Simulating modified circuit..."):
                modified_vout, modified_log, modified_netlist = simulate_divider(
                    original["vin"], original["r1"], fault_r2
                )
            st.session_state["fault_result"] = {
                "vin": original["vin"],
                "r1": original["r1"],
                "original_r2": original["r2"],
                "fault_r2": fault_r2,
                "original_vout": original["vout"],
                "modified_vout": modified_vout,
                "log": modified_log,
                "netlist": modified_netlist,
            }
            st.rerun()
        except Exception as error:
            st.error(f"Modified-circuit simulation failed: {error}")

    fault_result = st.session_state.get("fault_result")
    if fault_result and (
        fault_result["vin"], fault_result["r1"], fault_result["original_r2"], fault_result["fault_r2"]
    ) == (original["vin"], original["r1"], original["r2"], fault_r2):
        delta = fault_result["modified_vout"] - fault_result["original_vout"]
        st.markdown("### Before vs after")
        a, b, c = st.columns(3)
        a.metric("Original output", f'{fault_result["original_vout"]:.6f} V')
        b.metric("Modified output", f'{fault_result["modified_vout"]:.6f} V')
        c.metric("Output change", f"{delta:+.6f} V")

        compare = go.Figure()
        compare.add_bar(
            x=["Original circuit", "Modified circuit"],
            y=[fault_result["original_vout"], fault_result["modified_vout"]],
            text=[f'{fault_result["original_vout"]:.6f} V', f'{fault_result["modified_vout"]:.6f} V'],
            textposition="auto",
        )
        compare.update_layout(
            title="Fault-injection comparison",
            yaxis_title="Output voltage (V)",
            template="plotly_dark",
            height=320,
            margin=dict(l=20, r=20, t=55, b=20),
        )
        st.plotly_chart(compare, width="stretch")

        if abs(fault_result["fault_r2"] - fault_result["original_r2"]) < 1e-9:
            st.info("R2 is unchanged, so no resistor-value fault was introduced.")
        else:
            direction = "increased" if delta > 0 else "decreased" if delta < 0 else "did not change"
            percent = abs(delta) / max(abs(fault_result["original_vout"]), 1e-12) * 100
            st.warning(
                f"Diagnosis: R2 changed from {resistor_label(fault_result['original_r2'])} "
                f"to {resistor_label(fault_result['fault_r2'])}. The simulated output "
                f"{direction} by {abs(delta):.6f} V ({percent:.2f}% relative to the original output)."
            )
            st.caption(
                "This is a controlled resistor-value change and output comparison, not a claim "
                "of diagnosing arbitrary faults in an unknown physical circuit."
            )

        with st.expander("View modified SPICE netlist"):
            st.code(fault_result["netlist"], language="text")
        with st.expander("View modified ngspice output"):
            st.code(fault_result["log"], language="text")

st.markdown("---")
st.caption("CircuitMind V2 • ngspice-backed educational prototype. Validate designs before physical implementation.")
