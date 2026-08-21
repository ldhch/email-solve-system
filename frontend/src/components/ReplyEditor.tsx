import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { dataOf, errorText, http } from "../api/client";

interface ReplyTemplate {
  id: number;
  name: string;
  content: string;
  sort_order: number;
}

// One-click starter replies for the most common customer scenarios, now loaded
// from the backend so the boss can add/edit/delete them on the /templates page.
// Clicking a template fills the box; it is translated to English on send like
// any manually typed reply.
export function ReplyEditor({
  conversationId,
  onSent,
}: {
  conversationId: number;
  onSent: () => void;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [templates, setTemplates] = useState<ReplyTemplate[]>([]);

  useEffect(() => {
    let alive = true;
    http
      .get("/reply-templates")
      .then((resp) => {
        if (alive) setTemplates(dataOf<{ items: ReplyTemplate[] }>(resp).items);
      })
      .catch(() => {
        // templates are a convenience; the editor works fine without them
      });
    return () => {
      alive = false;
    };
  }, []);

  async function send() {
    if (!text.trim()) return;
    setError("");
    setSending(true);
    try {
      await http.post(`/conversations/${conversationId}/reply`, {
        content_cn: text.trim(),
      });
      setText("");
      onSent();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="bg-white rounded-lg border border-line p-4">
      <h3 className="text-sm font-medium text-ink mb-2">
        人工回复（中文，系统自动翻译为英文发送）
      </h3>
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <span className="text-[12px] text-sub">快捷模板：</span>
        {templates.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setText(t.content)}
            className="px-2 py-0.5 rounded border border-line text-[12px] text-sub hover:border-accent hover:text-accent"
          >
            {t.name}
          </button>
        ))}
        <Link
          to="/templates"
          className="px-2 py-0.5 rounded text-[12px] text-accent hover:underline"
        >
          ＋管理
        </Link>
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={4}
        className="w-full border border-line rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-accent/30"
        placeholder="在这里用中文输入回复内容…"
      />
      {error && <p className="text-sm text-risk-high mt-2">{error}</p>}
      <div className="mt-2 flex justify-end">
        <button
          onClick={send}
          disabled={sending || !text.trim()}
          className="px-4 py-2 bg-accent text-white rounded text-sm disabled:opacity-50"
        >
          {sending ? "发送中…" : "翻译并发送"}
        </button>
      </div>
    </div>
  );
}
