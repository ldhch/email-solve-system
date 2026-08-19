import { ReactNode, useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { dataOf, http } from "../api/client";

export function Layout({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    let alive = true;
    const fetchUnread = async () => {
      try {
        const resp = await http.get("/inbox/unread-count");
        if (alive) setUnread(dataOf<{ unread: number }>(resp).unread);
      } catch {
        // keep the last known count
      }
    };
    fetchUnread();
    const timer = setInterval(fetchUnread, 15000);
    const handleUnreadChanged = () => fetchUnread();
    window.addEventListener("inbox:unread-changed", handleUnreadChanged);
    return () => {
      alive = false;
      clearInterval(timer);
      window.removeEventListener("inbox:unread-changed", handleUnreadChanged);
    };
  }, []);

  async function logout() {
    try {
      await http.post("/auth/logout");
    } finally {
      navigate("/login", { replace: true });
    }
  }

  const linkCls = ({ isActive }: { isActive: boolean }) =>
    `px-3 py-2 rounded text-sm ${
      isActive ? "bg-accent text-white" : "text-sub hover:bg-[#F0F2F4] hover:text-ink"
    }`;

  return (
    <div className="min-h-screen">
      <header className="bg-white border-b border-line">
        <div className="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <span className="font-semibold text-accent tracking-tight">售后邮件后台</span>
            <nav className="flex gap-2">
              <NavLink to="/inbox" className={linkCls}>
                收件箱
                {unread > 0 && (
                  <span className="ml-1.5 inline-flex items-center px-1.5 py-0.5 rounded-full bg-risk-high text-white text-xs font-semibold">
                    {unread}
                  </span>
                )}
              </NavLink>
              <NavLink to="/tickets" className={linkCls}>
                工单
              </NavLink>
              <NavLink to="/knowledge" className={linkCls}>
                知识库
              </NavLink>
              <NavLink to="/qa" className={linkCls}>
                标准问答
              </NavLink>
              <NavLink to="/settings" className={linkCls}>
                设置
              </NavLink>
              <NavLink to="/audit" className={linkCls}>
                审计日志
              </NavLink>
            </nav>
          </div>
          <button
            onClick={logout}
            className="text-sm text-gray-500 hover:text-red-600"
          >
            退出登录
          </button>
        </div>
      </header>
      <main className="max-w-6xl mx-auto px-4 py-6">{children}</main>
    </div>
  );
}
