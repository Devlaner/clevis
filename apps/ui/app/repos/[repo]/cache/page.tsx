"use client";

import { useParams } from "next/navigation";
import { useState } from "react";

const API = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8080";

export default function CachePage() {
  const params = useParams<{ repo: string }>();
  const [token, setToken] = useState("");
  const [actor, setActor] = useState("admin@example.local");
  const [caches, setCaches] = useState<any[]>([]);
  const [result, setResult] = useState("");

  const [owner, repo] = (params.repo || "").split("~");

  async function loadCaches() {
    const res = await fetch(`${API}/repos/${owner}/${repo}/actions-caches`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    const json = await res.json();
    setCaches(json.actions_caches || []);
  }

  async function clearCaches(dryRun: boolean) {
    const res = await fetch(`${API}/repos/${owner}/${repo}/actions-caches/clear`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Role": "admin" },
      body: JSON.stringify({ token, actor, dry_run: dryRun }),
    });
    const json = await res.json();
    setResult(JSON.stringify(json));
  }

  return (
    <main>
      <h2>Actions Cache Management</h2>
      <p>Repository: {owner}/{repo}</p>
      <div style={{ display: "grid", gap: 8, maxWidth: 560 }}>
        <input placeholder="GitHub token" value={token} onChange={(e) => setToken(e.target.value)} />
        <input placeholder="Actor" value={actor} onChange={(e) => setActor(e.target.value)} />
        <button onClick={loadCaches}>Load caches</button>
        <button onClick={() => clearCaches(true)}>Dry run clear</button>
        <button onClick={() => clearCaches(false)}>Queue clear cache</button>
      </div>
      <ul>
        {caches.map((c) => (
          <li key={c.id}>{c.key} - {c.size_in_bytes} bytes</li>
        ))}
      </ul>
      {result ? <pre>{result}</pre> : null}
    </main>
  );
}
