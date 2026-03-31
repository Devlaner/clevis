"use client";

import { useState } from "react";

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8080";

export default function DashboardPage() {
  const [owner, setOwner] = useState("");
  const [token, setToken] = useState("");
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState("");

  async function runScan() {
    setError("");
    const res = await fetch(`${API}/analytics/overview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner, token }),
    });
    const json = await res.json();
    if (!res.ok) {
      setError(json.detail || "Scan failed");
      return;
    }
    setData(json);
  }

  return (
    <main>
      <h2>Security Analytics</h2>
      <p>Run checks for an organization and inspect posture.</p>
      <div style={{ display: "grid", gap: 8, maxWidth: 560 }}>
        <input placeholder="org login" value={owner} onChange={(e) => setOwner(e.target.value)} />
        <input placeholder="GitHub token" value={token} onChange={(e) => setToken(e.target.value)} />
        <button onClick={runScan}>Run analytics</button>
      </div>
      {error ? <p style={{ color: "red" }}>{error}</p> : null}
      {data ? (
        <section>
          <h3>Overview</h3>
          <p>Score: {data.score}</p>
          <p>Failed: {data.failed_checks} / {data.total_checks}</p>
          <ul>
            {data.checks.map((c: any) => (
              <li key={c.id}>
                <strong>{c.title}</strong> - {c.status.toUpperCase()} ({c.severity})
                <div>{c.remediation}</div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <hr />
      <p>Repository cache actions page format: <code>/repos/OWNER~REPO/cache</code></p>
    </main>
  );
}
