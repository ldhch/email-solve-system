import { ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { http } from "../api/client";

export function Layout({ children }: { children: ReactNode }) {
  const navigate = useNavigate();

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
        <div className="max-w-6xl mx-auto px-4 py-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-4">
            <span className="font-semibold text-accent tracking-tight">售后邮件后台</span>
            <nav className="flex gap-2">
              <NavLink to="/inbox" className={linkCls}>
                收件箱
              </NavLink>
              <NavLink to="/tickets" className={linkCls}>
                工单
              </NavLink>
              <NavLink to="/blocked" className={linkCls}>
                黑名单
              </NavLink>
              <NavLink to="/trash" className={linkCls}>
                回收站
              </NavLink>
              <NavLink to="/templates" className={linkCls}>
                快捷模板
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
                审计
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
