import Markdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';

/**
 * Sanitized markdown. `react-markdown` builds a React element tree (there is no
 * `dangerouslySetInnerHTML` sink), raw HTML is not enabled (no `rehype-raw`), and
 * `rehype-sanitize` applies the default allow-list on top. External links open in a new tab
 * with `rel="noopener noreferrer"`.
 */
export function MarkdownView({ children }: { children: string }): JSX.Element {
  return (
    <div className="markdown">
      <Markdown
        rehypePlugins={[rehypeSanitize]}
        components={{
          a: ({ href, title, children: linkChildren }) => {
            const external = typeof href === 'string' && /^https?:\/\//i.test(href);
            return (
              <a
                href={href}
                title={title}
                {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
              >
                {linkChildren}
              </a>
            );
          },
        }}
      >
        {children}
      </Markdown>
    </div>
  );
}
