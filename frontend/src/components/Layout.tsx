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
      isActive ? "bg-blue-600 text-white" : "text-gray-700 hover:bg-gray-200"
    }`;

  return (
    <div className="min-h-screen">
      <header className="bg-white border-b border-gray-200 shadow-sm">
        <div className="max-w-6xl mx-auto px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <span className="font-bold text-blue-700">售后邮件后台</span>
            <nav className="flex gap-2">
              <NavLink to="/inbox" className={linkCls}>
                收件箱
              </NavLink>
              <NavLink to="/tickets" className={linkCls}>
                工单
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
