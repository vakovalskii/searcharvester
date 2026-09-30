import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { mediaUrl } from "../lib/api";

interface Props {
  text: string;
  /** The job whose media may be shown; without it every image is a link. */
  jobId?: string | null;
}

const isHttp = (u: unknown) => /^https?:\/\//i.test(String(u ?? ""));

/** A picture through /media; if the adapter refuses it (not handed to this
 *  job, not an image, gone) it turns back into a plain link. */
function JobImage({ jobId, src, alt, title }: { jobId: string; src: string; alt: string; title?: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return <a href={src} target="_blank" rel="noopener noreferrer">[image: {alt || src}]</a>;
  }
  return (
    <img src={mediaUrl(jobId, src)} alt={alt} title={title || alt} loading="lazy" referrerPolicy="no-referrer"
         onError={() => setFailed(true)}
         className="inline-block max-w-full max-h-96 rounded-md border border-base-700 bg-base-900 align-middle" />
  );
}

/**
 * The one markdown renderer for agent text (report, findings, summaries): no
 * raw HTML, links only http(s) in a new tab, images only through /media of the
 * job. Stage D of docs/ui-and-visualization.md.
 */
export default function SafeMarkdown({ text, jobId }: Props) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      skipHtml
      components={{
        img: ({ src, alt, title }) =>
          jobId && isHttp(src)
            ? <JobImage jobId={jobId} src={String(src)} alt={String(alt ?? "")} title={title} />
            : <a href={isHttp(src) ? String(src) : undefined} target="_blank" rel="noopener noreferrer">[image: {alt || src}]</a>,
        a: ({ href, children, title }) =>
          isHttp(href) ? (
            <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a>
          ) : (
            <span>{children}</span>
          ),
      }}
    >
      {text}
    </ReactMarkdown>
  );
}
