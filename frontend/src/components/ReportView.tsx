import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy, Download, RotateCw } from "lucide-react";

interface Props {
  report: string;
  onRunAgain: () => void;
}

export default function ReportView({ report, onRunAgain }: Props) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(report);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const download = () => {
    const blob = new Blob([report], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "report.md";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="rounded-xl border border-base-700 bg-base-800/40 overflow-hidden">
      <div className="flex items-center justify-between px-5 py-3 border-b border-base-700">
        <div className="text-xs text-slate-500 uppercase tracking-wider font-semibold">
          Research report
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={copy}
            className="flex items-center gap-1.5 text-slate-400 hover:text-slate-100
                       px-3 py-1.5 rounded-md hover:bg-base-700/60 transition-colors text-xs"
            title="Copy markdown"
          >
            {copied ? <Check size={14} /> : <Copy size={14} />}
            <span>{copied ? "Copied" : "Copy MD"}</span>
          </button>
          <button
            onClick={download}
            className="flex items-center gap-1.5 text-slate-400 hover:text-slate-100
                       px-3 py-1.5 rounded-md hover:bg-base-700/60 transition-colors text-xs"
            title="Download .md"
          >
            <Download size={14} />
            <span>Download</span>
          </button>
          <button
            onClick={onRunAgain}
            className="flex items-center gap-1.5 text-slate-400 hover:text-slate-100
                       px-3 py-1.5 rounded-md hover:bg-base-700/60 transition-colors text-xs"
            title="New research"
          >
            <RotateCw size={14} />
            <span>New</span>
          </button>
        </div>
      </div>
      <article className="md-body px-6 py-5 overflow-x-auto">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          skipHtml
          components={{
            // Until /media (stage D) the browser must not fetch images from other sites.
            img: ({ src, alt }) => (
              <a href={String(src ?? "")} target="_blank" rel="noopener noreferrer">[image: {alt || src}]</a>
            ),
            a: ({ href, children }) =>
              /^https?:/i.test(String(href ?? "")) ? (
                <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
              ) : (
                <span>{children}</span>
              ),
          }}
        >
          {report}
        </ReactMarkdown>
      </article>
    </div>
  );
}
