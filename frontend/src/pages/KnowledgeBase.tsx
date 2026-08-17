import { useCallback, useEffect, useRef, useState } from "react";
import { dataOf, errorText, http } from "../api/client";
import { Layout } from "../components/Layout";

interface KbDoc {
  id: number;
  filename: string;
  version: number;
  uploaded_at: string | null;
}

export default function KnowledgeBase() {
  const [items, setItems] = useState<KbDoc[]>([]);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const resp = await http.get("/kb/docs");
      setItems(dataOf<{ items: KbDoc[] }>(resp).items);
    } catch {
      setError("加载知识库失败");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function upload(file: File) {
    if (!/\.(pdf|docx|md)$/i.test(file.name)) {
      setError("仅支持 PDF / DOCX / MD 文件");
      return;
    }
    if (file.size > 20 * 1024 * 1024) {
      setError("文件不能超过 20MB");
      return;
    }
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const form = new FormData();
      form.append("file", file);
      const resp = await http.post("/kb/upload", form);
      const { doc_id, version } = dataOf<{ doc_id: number; version: number }>(resp);
      setSuccess(`上传成功：文档 #${doc_id}（版本 v${version}）`);
      await load();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function remove(doc: KbDoc) {
    if (!window.confirm(`确定删除「${doc.filename}」？删除后回复生成不再使用该文档。`)) {
      return;
    }
    setError("");
    setSuccess("");
    try {
      await http.delete(`/kb/docs/${doc.id}`);
      setSuccess(`已删除「${doc.filename}」`);
      await load();
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <Layout>
      <h1 className="text-lg font-bold mb-4">知识库</h1>
      <p className="text-sm text-gray-500 mb-4">
        上传 PDF / DOCX / MD（≤20MB），系统提取全文并在回复生成时全文注入；
        重复上传同名文件会覆盖并升级版本号。
      </p>

      {error && <p className="text-sm text-red-600 mb-3">{error}</p>}
      {success && <p className="text-sm text-green-600 mb-3">{success}</p>}

      <label
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          const file = e.dataTransfer.files?.[0];
          if (file) upload(file);
        }}
        className={`block border-2 border-dashed rounded-lg p-8 text-center cursor-pointer mb-6 ${
          dragging ? "border-blue-500 bg-blue-50" : "border-gray-300 bg-white"
        }`}
      >
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.docx,.md"
          className="hidden"
          disabled={busy}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) upload(file);
          }}
        />
        <span className="text-sm text-gray-600">
          {busy ? "上传中…" : "点击选择或拖拽文件到此处上传（PDF / DOCX / MD）"}
        </span>
      </label>

      {items.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-10 text-center text-gray-500">
          暂无知识库文档
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-left text-gray-500">
              <tr>
                <th className="px-4 py-2">文件名</th>
                <th className="px-4 py-2">版本</th>
                <th className="px-4 py-2">上传时间</th>
                <th className="px-4 py-2">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((doc) => (
                <tr key={doc.id} className="border-t border-gray-100 hover:bg-blue-50">
                  <td className="px-4 py-2 font-medium">{doc.filename}</td>
                  <td className="px-4 py-2 text-gray-600">v{doc.version}</td>
                  <td className="px-4 py-2 text-gray-500">
                    {doc.uploaded_at ? doc.uploaded_at.replace("T", " ").replace("Z", "") : "—"}
                  </td>
                  <td className="px-4 py-2">
                    <button
                      onClick={() => remove(doc)}
                      className="px-2 py-1 border border-red-300 text-red-600 rounded text-xs hover:bg-red-50"
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Layout>
  );
}
