import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { go } from "../lib/nav";
import { results, resultSvgs } from "../lib/docs";

/** Repo markdown, with repo-relative links turned into in-app links where the app has the file. */
export default function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          img: ({ src = "", alt }) => <img src={resultSvgs[src.split("/").pop()!] ?? src} alt={alt ?? ""} loading="lazy" />,
          a: ({ href = "", children }) => {
            const file = href.split("/").pop()!.split("#")[0];
            const adr = /^(\d{4})-.*\.md$/.exec(file);
            const target = adr ? (["decisions", adr[1]] as const)
              : results[file] ? (["evidence", `file:${file}`] as const)
              : null;
            if (target) {
              return <a href={`#${target[0]}/${encodeURIComponent(target[1])}`} onClick={(e) => { e.preventDefault(); go(target[0], target[1]); }}>{children}</a>;
            }
            if (/^https?:/.test(href)) return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
            return <code title="file in the repo">{children}</code>;
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
