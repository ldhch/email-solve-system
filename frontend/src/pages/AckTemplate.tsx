import { useCallback, useEffect, useState } from "react";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";

interface AckTemplate {
  content_cn: string;
  content_en: string;
  /** True: English follows CN (auto-translated on save). False: hand-tuned. */
  content_en_auto: boolean;
  updated_at: string | null;
}

/**
 * 自动回复模板编辑器：中文/英文双栏填满视口，避免频繁滚动。
 *
 * 邮件需要人工审核时，系统自动发送这条固定确认回复给客户。中文是老板在
 * 后台读写的版本，英文是实际发给客户的内容。保存时英文没被手动改过就自动
 * 按中文翻译；一旦手动改过英文，就以英文为准、不再被中文覆盖。
 */
export default function AckTemplate() {
  const [ackTemplate, setAckTemplate] = useState<AckTemplate | null>(null);
  const [ackCn, setAckCn] = useState("");
  const [ackEn, setAckEn] = useState("");
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const resp = await http.get("/system/ack-template");
      setAckTemplate(dataOf<AckTemplate>(resp));
    } catch {
      setError("加载模板失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Sync the editors once the template loads / after saving.
  useEffect(() => {
    if (!ackTemplate) return;
    setAckCn(ackTemplate.content_cn);
    setAckEn(ackTemplate.content_en);
  }, [ackTemplate]);

  async function saveAckTemplate() {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      // English follows CN when: the saved template is auto-managed AND the
      // English box wasn't hand-edited (still equals the loaded value). Then we
      // submit CN only and the backend re-translates. Any manual English edit
      // locks the English box as the source of truth.
      const autoManaged =
        ackTemplate !== null &&
        ackTemplate.content_en_auto &&
        ackEn === ackTemplate.content_en;
      await http.put("/system/ack-template", {
        content_cn: ackCn,
        ...(autoManaged ? {} : { content_en: ackEn }),
      });
      setSuccess(autoManaged ? "已保存，英文已按中文自动翻译" : "已保存");
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Layout>
      <div
        className="flex flex-col"
        style={{ height: "calc(100vh - 170px)", minHeight: 420 }}
      >
        {/* 操作栏 */}
        <div className="flex flex-wrap items-start justify-between gap-3 shrink-0 mb-3">
          <div>
            <h1 className="text-lg font-bold mb-1">自动回复模板</h1>
            <p className="text-sm text-gray-600">
              邮件需要人工审核时，系统自动发送这条固定确认回复给客户。支持{" "}
              {"{customer_name}"} 占位符（发送时替换为客户显示名称）。
              <span className="ml-2 text-gray-400">
                {ackTemplate?.updated_at
                  ? `上次更新：${ackTemplate.updated_at.replace("T", " ").replace("Z", "")}`
                  : "当前使用系统默认模板"}
              </span>
            </p>
          </div>
          <button
            type="button"
            onClick={saveAckTemplate}
            disabled={busy}
            className="px-4 py-2 bg-blue-600 text-white rounded text-sm hover:bg-blue-700 disabled:opacity-50 shrink-0"
          >
            保存模板
          </button>
        </div>

        {error && <p className="text-sm text-red-600 mb-3 shrink-0">{error}</p>}
        {success && <p className="text-sm text-green-600 mb-3 shrink-0">{success}</p>}

        {/* 双栏编辑区：填满剩余高度，滚动条只在内容超出时出现 */}
        <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-2 gap-4">
          <div className="flex flex-col min-h-0">
            <label className="mb-1 text-sm text-gray-600 shrink-0">
              中文内容
            </label>
            <textarea
              value={ackCn}
              onChange={(e) => setAckCn(e.target.value)}
              placeholder={
                "感谢您联系 LBORA。\n\n我们已经收到您的邮件，正在处理中，会在 1-2 个工作日内回复。"
              }
              className="flex-1 min-h-0 w-full border border-gray-300 rounded text-sm p-3 font-mono resize-none focus:outline-none focus:ring-2 focus:ring-accent/30"
            />
          </div>
          <div className="flex flex-col min-h-0">
            <label className="mb-1 text-sm text-gray-600 shrink-0">
              英文内容（实际发送给客户）
              <span className="ml-2 text-gray-400 font-normal">
                未修改时保存自动按中文翻译；手动改过则以英文为准（清空可恢复自动翻译）
              </span>
            </label>
            <textarea
              value={ackEn}
              onChange={(e) => setAckEn(e.target.value)}
              placeholder={"Hi {customer_name},\n\nThank you for contacting LBORA."}
              className="flex-1 min-h-0 w-full border border-gray-300 rounded text-sm p-3 font-mono resize-none focus:outline-none focus:ring-2 focus:ring-accent/30"
            />
          </div>
        </div>
      </div>
    </Layout>
  );
}
