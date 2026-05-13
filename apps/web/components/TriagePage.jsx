"use client";

import { useState, useEffect, useRef } from "react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const LEVEL_CONFIG = {
  RED:        { bg: "bg-red-600",    border: "border-red-500",    label: "IMMEDIATE",  sub: "Act within 60 seconds" },
  YELLOW:     { bg: "bg-yellow-500", border: "border-yellow-400", label: "DELAYED",    sub: "Act within 30 minutes" },
  GREEN:      { bg: "bg-green-600",  border: "border-green-500",  label: "MINOR",      sub: "Queue — walking wounded" },
  BLACK:      { bg: "bg-gray-800",   border: "border-gray-600",   label: "EXPECTANT",  sub: "Unsurvivable w/ available resources" },
  SPECIALIST: { bg: "bg-purple-700", border: "border-purple-500", label: "SPECIALIST", sub: "Use physical specialist manual" },
};

const EMPTY_VITALS = { hr: "", rr: "", bp_sys: "", spo2: "", gcs: "" };

export default function TriagePage() {
  const [vitals, setVitals]           = useState(EMPTY_VITALS);
  const [description, setDescription] = useState("");
  const [scene, setScene]             = useState("");
  const [result, setResult]           = useState(null);
  const [streaming, setStreaming]      = useState(false);
  const [streamTokens, setStreamTokens] = useState("");
  const [health, setHealth]           = useState(null);
  const [latency, setLatency]         = useState(null);
  const [detCheck, setDetCheck]       = useState(null);
  const timerRef = useRef(null);

  // ── Poll system health every 5s ─────────────────────────────────────────
  useEffect(() => {
    const poll = async () => {
      try {
        const r = await fetch(`${API}/system/health`);
        setHealth(await r.json());
      } catch { setHealth(null); }
    };
    poll();
    const id = setInterval(poll, 5000);
    return () => clearInterval(id);
  }, []);

  // ── Streaming triage ─────────────────────────────────────────────────────
  const runTriage = async () => {
    setResult(null);
    setStreamTokens("");
    setDetCheck(null);
    setLatency(null);
    setStreaming(true);
    const t0 = performance.now();

    const body = {
      patient_id:    `P-${Date.now()}`,
      description,
      scene_context: scene,
      vitals: {
        hr:     vitals.hr     ? parseFloat(vitals.hr)     : null,
        rr:     vitals.rr     ? parseFloat(vitals.rr)     : null,
        bp_sys: vitals.bp_sys ? parseFloat(vitals.bp_sys) : null,
        spo2:   vitals.spo2   ? parseFloat(vitals.spo2)   : null,
        gcs:    vitals.gcs    ? parseInt(vitals.gcs)      : null,
      },
    };

    try {
      const resp = await fetch(`${API}/triage/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });

        const lines = buf.split("\n");
        buf = lines.pop();

        for (const line of lines) {
          if (line.startsWith("data:")) {
            const data = JSON.parse(line.slice(5).trim());
            if (line.startsWith("event: deterministic") || data.level) {
              setDetCheck(data);
            } else if (data.text) {
              setStreamTokens(prev => prev + data.text);
            } else if (data.total_ms !== undefined) {
              setResult(data);
              setLatency(Math.round(performance.now() - t0));
            }
          }
        }
      }
    } catch (e) {
      setResult({ error: e.message });
    }
    setStreaming(false);
  };

  const levelCfg = result?.level
    ? LEVEL_CONFIG[result.level] || LEVEL_CONFIG.YELLOW
    : null;

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 font-mono">

      {/* ── Header ─────────────────────────────────────────────────────── */}
      <header className="border-b border-gray-800 px-6 py-4 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">
            <span className="text-red-500">⬤</span> RescueNet
          </h1>
          <p className="text-xs text-gray-500 mt-0.5">
            Offline Medical Triage · Gemma 4 · WHO ETAT 2016
          </p>
        </div>

        {/* Offline badge */}
        <div className="flex items-center gap-3">
          {health && (
            <span className={`text-xs font-bold px-3 py-1 rounded-full border ${
              health.offline_badge
                ? "bg-green-900/40 border-green-600 text-green-400"
                : "bg-yellow-900/40 border-yellow-600 text-yellow-400"
            }`}>
              {health.offline_badge ? "● C-BLACKOUT / OFFLINE" : "◯ ONLINE"}
            </span>
          )}
        </div>
      </header>

      <div className="max-w-5xl mx-auto px-6 py-6 grid grid-cols-1 lg:grid-cols-3 gap-6">

        {/* ── Left: Input form ──────────────────────────────────────────── */}
        <div className="lg:col-span-2 space-y-4">

          {/* Vitals */}
          <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <h2 className="text-xs font-bold uppercase tracking-widest text-gray-400 mb-4">
              Patient Vitals
            </h2>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
              {[
                { key: "hr",     label: "Heart Rate",    unit: "bpm",   placeholder: "e.g. 110" },
                { key: "rr",     label: "Resp. Rate",    unit: "br/min",placeholder: "e.g. 24"  },
                { key: "bp_sys", label: "BP Systolic",   unit: "mmHg",  placeholder: "e.g. 85"  },
                { key: "spo2",   label: "SpO₂",          unit: "%",     placeholder: "e.g. 92"  },
                { key: "gcs",    label: "GCS",           unit: "3–15",  placeholder: "e.g. 12"  },
              ].map(({ key, label, unit, placeholder }) => (
                <div key={key}>
                  <label className="text-xs text-gray-500 mb-1 block">
                    {label} <span className="text-gray-600">({unit})</span>
                  </label>
                  <input
                    type="number"
                    placeholder={placeholder}
                    value={vitals[key]}
                    onChange={e => setVitals(v => ({ ...v, [key]: e.target.value }))}
                    className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:border-red-500 focus:outline-none"
                  />
                </div>
              ))}
            </div>
          </section>

          {/* Description + scene */}
          <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <h2 className="text-xs font-bold uppercase tracking-widest text-gray-400 mb-4">
              Clinical Presentation
            </h2>
            <textarea
              rows={3}
              placeholder="Describe symptoms, mechanism of injury, presentation..."
              value={description}
              onChange={e => setDescription(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:border-red-500 focus:outline-none resize-none mb-3"
            />
            <input
              type="text"
              placeholder="Scene context (e.g. earthquake, building collapse, road traffic)"
              value={scene}
              onChange={e => setScene(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-100 focus:border-red-500 focus:outline-none"
            />
          </section>

          {/* Submit */}
          <button
            onClick={runTriage}
            disabled={streaming || (!description && Object.values(vitals).every(v => !v))}
            className="w-full bg-red-600 hover:bg-red-700 disabled:bg-gray-700 disabled:text-gray-500 text-white font-bold py-3 rounded-xl transition-colors text-sm tracking-wide"
          >
            {streaming ? "⟳ Analysing..." : "RUN TRIAGE"}
          </button>

          {/* Deterministic check flash */}
          {detCheck && (
            <div className={`border rounded-xl px-5 py-3 text-sm ${
              detCheck.check === "PASSED" || !detCheck.discrepancy
                ? "bg-green-900/20 border-green-700 text-green-300"
                : "bg-yellow-900/20 border-yellow-600 text-yellow-300"
            }`}>
              <span className="font-bold text-xs uppercase tracking-widest block mb-1">
                Deterministic Check · {detCheck.check || "PASSED"}
              </span>
              <span className="text-xs text-gray-400">
                Rule engine: <span className="font-bold text-gray-200">{detCheck.level}</span>
                {" "}· conf {Math.round((detCheck.confidence || 0) * 100)}%
                {detCheck.alerts?.length > 0 && (
                  <span className="text-red-400 ml-2">⚠ {detCheck.alerts[0]}</span>
                )}
              </span>
            </div>
          )}

          {/* Streaming tokens */}
          {streamTokens && (
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 text-xs text-gray-400 leading-relaxed max-h-40 overflow-y-auto">
              <span className="text-xs uppercase tracking-widest text-gray-600 block mb-2">
                Gemma 4 · Live output
              </span>
              {streamTokens}
              {streaming && <span className="animate-pulse">▌</span>}
            </div>
          )}

          {/* Final result */}
          {result && !result.error && levelCfg && (
            <div className={`border ${levelCfg.border} rounded-xl overflow-hidden`}>
              {/* Level header */}
              <div className={`${levelCfg.bg} px-5 py-4 flex items-center justify-between`}>
                <div>
                  <div className="text-2xl font-black tracking-wider">
                    {result.level}
                  </div>
                  <div className="text-xs opacity-80 mt-0.5">{levelCfg.label} · {levelCfg.sub}</div>
                </div>
                <div className="text-right text-xs opacity-70">
                  <div>{latency}ms total</div>
                  <div>{result.total_ms}ms inference</div>
                </div>
              </div>

              {/* Deterministic badge */}
              <div className={`px-5 py-2 text-xs font-bold border-b ${levelCfg.border} ${
                result.deterministic_check === "PASSED"
                  ? "bg-green-900/20 text-green-400"
                  : "bg-yellow-900/20 text-yellow-400"
              }`}>
                DETERMINISTIC CHECK: {result.deterministic_check || "PASSED"}
                {result.deterministic_check !== "PASSED" && (
                  <span className="text-yellow-500 ml-2">→ MANUAL REVIEW REQUIRED</span>
                )}
              </div>

              {/* Actions */}
              {result.actions?.length > 0 && (
                <div className="px-5 py-4 border-b border-gray-800">
                  <div className="text-xs uppercase tracking-widest text-gray-500 mb-3">
                    Immediate Actions
                  </div>
                  <ol className="space-y-2">
                    {result.actions.map((a, i) => (
                      <li key={i} className="flex gap-3 text-sm">
                        <span className="text-red-400 font-bold min-w-[20px]">{i + 1}.</span>
                        <span className={a.startsWith("⚠") ? "text-yellow-300" : "text-gray-200"}>
                          {a}
                        </span>
                      </li>
                    ))}
                  </ol>
                </div>
              )}

              {/* Protocol ref + dosage */}
              <div className="px-5 py-3 flex items-center justify-between text-xs text-gray-500">
                <span>
                  Protocol: <span className="text-gray-300">{result.protocol_ref || "WHO_ETAT_2016"}</span>
                </span>
                <span className={result.dosage_safe === false ? "text-red-400" : "text-green-400"}>
                  {result.dosage_safe === false ? "⚠ DOSAGE FLAGGED" : "✓ Dosage safe"}
                </span>
              </div>
            </div>
          )}

          {result?.error && (
            <div className="bg-red-900/20 border border-red-700 rounded-xl px-5 py-4 text-red-300 text-sm">
              Error: {result.error}
            </div>
          )}
        </div>

        {/* ── Right: System health panel ──────────────────────────────── */}
        <div className="space-y-4">
          <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <h2 className="text-xs font-bold uppercase tracking-widest text-gray-400 mb-4">
              System Health
            </h2>
            {health ? (
              <div className="space-y-4">

                {/* Connectivity */}
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-500">Connectivity</span>
                  <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                    health.offline_badge ? "bg-green-900 text-green-300" : "bg-yellow-900 text-yellow-300"
                  }`}>
                    {health.connectivity}
                  </span>
                </div>

                {/* Model */}
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-500">Gemma 4 loaded</span>
                  <span className={`text-xs font-bold ${health.model_loaded ? "text-green-400" : "text-red-400"}`}>
                    {health.model_loaded ? "● READY" : "○ NOT LOADED"}
                  </span>
                </div>

                {/* Qdrant */}
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-500">Qdrant</span>
                  <span className={`text-xs font-bold ${health.qdrant_ready ? "text-green-400" : "text-red-400"}`}>
                    {health.qdrant_ready ? "● READY" : "○ DOWN"}
                  </span>
                </div>

                {/* Redis */}
                <div className="flex items-center justify-between">
                  <span className="text-xs text-gray-500">Redis cache</span>
                  <span className={`text-xs font-bold ${health.redis_ready ? "text-green-400" : "text-red-400"}`}>
                    {health.redis_ready ? "● READY" : "○ DOWN"}
                  </span>
                </div>

                {/* RAM bar */}
                <div>
                  <div className="flex justify-between text-xs text-gray-500 mb-1">
                    <span>RAM</span>
                    <span>{health.ram_used_gb}GB / {health.ram_total_gb}GB</span>
                  </div>
                  <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${
                        health.ram_percent > 85 ? "bg-red-500" :
                        health.ram_percent > 65 ? "bg-yellow-500" : "bg-green-500"
                      }`}
                      style={{ width: `${health.ram_percent}%` }}
                    />
                  </div>
                </div>

                {/* CPU */}
                <div>
                  <div className="flex justify-between text-xs text-gray-500 mb-1">
                    <span>CPU</span>
                    <span>{health.cpu_percent}%
                      {health.cpu_temp_c && ` · ${health.cpu_temp_c}°C`}
                    </span>
                  </div>
                  <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${
                        health.cpu_percent > 85 ? "bg-red-500" :
                        health.cpu_percent > 60 ? "bg-yellow-500" : "bg-green-500"
                      }`}
                      style={{ width: `${health.cpu_percent}%` }}
                    />
                  </div>
                </div>

                {health.cpu_temp_c && (
                  <div>
                    <div className="flex justify-between text-xs text-gray-500 mb-1">
                      <span>Temp</span>
                      <span>{health.cpu_temp_c}°C</span>
                    </div>
                    <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
                      <div
                        className={`h-full rounded-full ${
                          health.cpu_temp_c > 80 ? "bg-red-500" :
                          health.cpu_temp_c > 65 ? "bg-yellow-500" : "bg-green-500"
                        }`}
                        style={{ width: `${Math.min(health.cpu_temp_c, 100)}%` }}
                      />
                    </div>
                  </div>
                )}

              </div>
            ) : (
              <p className="text-xs text-gray-600">Connecting to API...</p>
            )}
          </section>

          {/* Latency card */}
          {latency && (
            <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
              <h2 className="text-xs font-bold uppercase tracking-widest text-gray-400 mb-3">
                Last Triage
              </h2>
              <div className="text-3xl font-black text-white">{latency}<span className="text-sm font-normal text-gray-500">ms</span></div>
              <div className="text-xs text-gray-500 mt-1">End-to-end latency</div>
              <div className={`text-xs mt-2 font-bold ${latency < 2000 ? "text-green-400" : "text-yellow-400"}`}>
                {latency < 2000 ? "✓ Under 2s target" : "⚠ Over 2s target"}
              </div>
            </section>
          )}

          {/* Demo tip */}
          <section className="bg-gray-900 border border-dashed border-gray-700 rounded-xl p-4">
            <p className="text-xs text-gray-600 leading-relaxed">
              <span className="text-gray-400 font-bold block mb-1">Demo tip</span>
              Disconnect ethernet/wifi mid-query. System keeps running.
              Offline badge turns green. Response still arrives in &lt;2s.
            </p>
          </section>
        </div>

      </div>
    </div>
  );
}
