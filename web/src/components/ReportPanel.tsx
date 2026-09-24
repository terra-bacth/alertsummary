import { useEffect, useState } from 'react';
import DOMPurify from 'dompurify';
import { marked } from 'marked';
import type { Report } from '../lib/api';

function renderMarkdown(md: string): string {
  const html = marked.parse(md, { async: false }) as string;
  return DOMPurify.sanitize(html);
}

export function ReportPanel({ report }: { report?: Report }) {
  const [html, setHtml] = useState<string>('');

  useEffect(() => {
    if (report?.markdown) {
      setHtml(renderMarkdown(report.markdown));
    }
  }, [report]);

  if (!report) {
    return (
      <div className="report-body">
        <div className="stream-empty" style={{ height: '100%', display: 'grid', placeItems: 'center' }}>
          <div>
            <span className="big">📋</span>
            <br />
            No report yet — the report agent writes here when the investigation closes.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="report-body">
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          marginBottom: 12,
          fontFamily: 'var(--mono)',
          fontSize: 12,
          color: 'var(--ink-dim)',
        }}
      >
        <span>ROOT CAUSE CONFIDENCE</span>
        <div
          style={{
            flex: 1,
            height: 6,
            borderRadius: 999,
            background: 'var(--line)',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              width: `${Math.round(report.confidence * 100)}%`,
              height: '100%',
              background: 'linear-gradient(90deg, var(--amber), var(--green))',
              transition: 'width .8s ease',
            }}
          />
        </div>
        <strong style={{ color: 'var(--green)' }}>{Math.round(report.confidence * 100)}%</strong>
      </div>
      <div dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}
