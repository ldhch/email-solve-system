import { useState } from "react";
import { errorText, http } from "../api/client";

// One-click starter replies for the most common customer scenarios. The boss
// pastes one and adjusts it; it is translated to English on send like any
// manually typed reply.
const TEMPLATES: { label: string; text: string }[] = [
  {
    label: "退货",
    text: "非常抱歉给您带来不便。您可以退回商品，我们会为您全额退款。请回复您的订单号，我们会通过邮件发送退货标签和详细指引。",
  },
  {
    label: "物流",
    text: "感谢您的耐心等待。我已为您查询物流，包裹正在途中，预计很快送达。若仍无更新，我们会继续为您跟进。",
  },
  {
    label: "补偿",
    text: "非常抱歉给您带来的不便。为表歉意，我们将为您申请补偿。请回复确认，我们会尽快为您处理。",
  },
  {
    label: "通用",
    text: "感谢您联系我们。我们会尽快处理您的问题，并在一到两个工作日内给您回复。",
  },
];

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
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      <h3 className="text-sm font-medium text-gray-700 mb-2">
        人工回复（中文，系统自动翻译为英文发送）
      </h3>
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <span className="text-[12px] text-sub">快捷模板：</span>
        {TEMPLATES.map((t) => (
          <button
            key={t.label}
            type="button"
            onClick={() => setText(t.text)}
            className="px-2 py-0.5 rounded border border-line text-[12px] text-sub hover:border-accent hover:text-accent"
          >
            {t.label}
          </button>
        ))}
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={4}
        className="w-full border border-gray-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        placeholder="在这里用中文输入回复内容…"
      />
      {error && <p className="text-sm text-red-600 mt-2">{error}</p>}
      <div className="mt-2 flex justify-end">
        <button
          onClick={send}
          disabled={sending || !text.trim()}
          className="px-4 py-2 bg-blue-600 text-white rounded text-sm disabled:opacity-50"
        >
          {sending ? "发送中…" : "翻译并发送"}
        </button>
      </div>
    </div>
  );
}
