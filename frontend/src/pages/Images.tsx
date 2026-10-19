import { useCallback, useEffect, useState } from "react";
import { api, Image, ImageStatus } from "../lib/api";
import { useToast } from "../components/Toast";
import { humanBytes } from "../lib/format";

const STATUSES: ImageStatus[] = ["downloaded", "pending", "removed", "failed"];

export default function Images() {
  const toast = useToast();
  const [items, setItems] = useState<Image[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<Record<ImageStatus, number> | null>(null);
  const [page, setPage] = useState(1);
  const [team, setTeam] = useState("");
  const [year, setYear] = useState("");
  const [status, setStatus] = useState("");
  const pageSize = 60;

  const load = useCallback(async () => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (team) params.set("team", team);
    if (year) params.set("year", year);
    if (status) params.set("status", status);
    try {
      const res = await api.images(params.toString());
      setItems(res.items);
      setTotal(res.total);
      setCounts(res.counts);
    } catch (e: any) { toast(e.message, true); }
  }, [page, team, year, status, toast]);

  useEffect(() => { load(); }, [load]);

  const pages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <>
      <div className="page-head">
        <h1>Images</h1>
        <span className="muted">{total} images</span>
      </div>

      <div className="grid stats">
        {STATUSES.map((s) => (
          <div key={s} className="card stat">
            <div className="label">{s}</div>
            <div className="value">{counts ? counts[s] : "—"}</div>
          </div>
        ))}
      </div>

      <div className="toolbar">
        <input placeholder="Team number" inputMode="numeric" value={team}
          onChange={(e) => { setPage(1); setTeam(e.target.value.replace(/\D/g, "")); }} />
        <input placeholder="Year" inputMode="numeric" value={year}
          onChange={(e) => { setPage(1); setYear(e.target.value.replace(/\D/g, "")); }} />
        <select value={status} onChange={(e) => { setPage(1); setStatus(e.target.value); }}>
          <option value="">All statuses</option>
          {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>

      <div className="image-grid">
        {items.map((img) => (
          <figure key={img.id} className="card image-card">
            <a href={`https://imgur.com/${img.foreign_key}`} target="_blank" rel="noreferrer">
              {img.status === "downloaded"
                ? <img loading="lazy" src={`/api/images/${img.id}/file`} alt={`Team ${img.team_number} ${img.year}`} />
                : <div className="image-placeholder"><span className={`badge ${img.status}`}>{img.status}</span></div>}
            </a>
            <figcaption>
              <strong>{img.team_number}</strong> <span className="muted">{img.year}</span>
              {img.preferred && <> <span className="badge preferred">preferred</span></>}
              <div className="mono muted">{img.foreign_key} · {humanBytes(img.size)}</div>
              {img.error && <div className="mono muted" title={img.error}>{img.error}</div>}
            </figcaption>
          </figure>
        ))}
      </div>
      {items.length === 0 && <div className="empty">No images match.</div>}

      <div className="row" style={{ justifyContent: "flex-end", marginTop: 16, gap: 12 }}>
        <button className="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>Prev</button>
        <span className="muted">Page {page} / {pages}</span>
        <button className="sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
      </div>
    </>
  );
}
