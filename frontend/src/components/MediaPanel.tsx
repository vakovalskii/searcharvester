import { useEffect, useState } from "react";
import { Image as ImageIcon, PlayCircle } from "lucide-react";
import type { MediaItem } from "../lib/api";
import { getJobMedia, mediaUrl } from "../lib/api";

interface Props {
  jobId: string;
  live: boolean;          // the job still runs: look again now and then
  report: string | null;  // to mark what made it into the report
}

function Preview({ jobId, src, alt }: { jobId: string; src: string | null; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) {
    return <div className="w-full h-full flex items-center justify-center text-slate-600"><ImageIcon size={22} /></div>;
  }
  return <img src={mediaUrl(jobId, src)} alt={alt} loading="lazy" referrerPolicy="no-referrer"
              onError={() => setFailed(true)} className="w-full h-full object-cover" />;
}

function Tile({ jobId, m, cited }: { jobId: string; m: MediaItem; cited: boolean }) {
  const video = m.kind === "video";
  return (
    <a href={video ? m.src : m.page} target="_blank" rel="noopener noreferrer"
       className={`group block rounded-lg border overflow-hidden bg-base-900/60 hover:border-accent-500/70
                   ${cited ? "border-emerald-500/50" : "border-base-700"}`}>
      <div className="relative aspect-video bg-base-950">
        <Preview jobId={jobId} src={video ? m.thumb : (m.thumb ?? m.src)} alt={m.title} />
        {video && <PlayCircle size={28} className="absolute inset-0 m-auto text-white/80 drop-shadow" />}
        {video && m.duration && (
          <span className="absolute right-1.5 bottom-1.5 rounded bg-black/70 px-1 text-[10px] font-mono text-slate-100">{m.duration}</span>
        )}
      </div>
      <div className="px-2 py-1.5">
        <div className="text-xs text-slate-200 line-clamp-2">{m.title || m.page}</div>
        <div className="mt-0.5 flex items-center gap-2 text-[10px] font-mono text-slate-500">
          <span className="truncate">{(() => { try { return new URL(m.page).hostname.replace(/^www\./, ""); } catch { return ""; } })()}</span>
          {m.size && <span>{m.size}</span>}
          {cited && <span className="ml-auto text-emerald-400">in report</span>}
        </div>
      </div>
    </a>
  );
}

/** Every image and video search gave the job's agents: what they could show. */
export default function MediaPanel({ jobId, live, report }: Props) {
  const [items, setItems] = useState<MediaItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () => getJobMedia(jobId).then((x) => alive && setItems(x)).catch((e: Error) => alive && setError(e.message));
    load();
    const t = live ? window.setInterval(load, 10000) : undefined;
    return () => { alive = false; if (t) window.clearInterval(t); };
  }, [jobId, live]);

  if (error) return <div className="text-sm text-red-300">Media list unavailable: {error}</div>;
  if (items === null) return <div className="text-sm text-slate-500">Loading…</div>;
  if (items.length === 0) {
    return <div className="text-sm text-slate-500">
      {live ? "No images or videos found yet." : "The agents did not search for images or videos in this research."}
    </div>;
  }
  const inReport = (m: MediaItem) => Boolean(report && (report.includes(m.src) || (m.thumb && report.includes(m.thumb))));
  const groups: [string, MediaItem[]][] = [["Videos", items.filter((m) => m.kind === "video")],
                                           ["Images", items.filter((m) => m.kind === "image")]];
  return (
    <div className="space-y-4">
      {groups.filter(([, g]) => g.length).map(([name, g]) => (
        <section key={name}>
          <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1.5">{name} · {g.length}</div>
          <div className="grid gap-2 grid-cols-[repeat(auto-fill,minmax(10rem,1fr))]">
            {g.map((m) => <Tile key={m.src} jobId={jobId} m={m} cited={inReport(m)} />)}
          </div>
        </section>
      ))}
      <div className="text-[11px] text-slate-500">Pictures come through the adapter; only what search returned to this research is shown.</div>
    </div>
  );
}
