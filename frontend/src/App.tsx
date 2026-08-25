import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "./components/AuthGuard";
import AuditLogs from "./pages/AuditLogs";
import BlockedSenders from "./pages/BlockedSenders";
import ConversationDetail from "./pages/ConversationDetail";
import Inbox from "./pages/Inbox";
import KnowledgeBase from "./pages/KnowledgeBase";
import Login from "./pages/Login";
import QAPairs from "./pages/QAPairs";
import Settings from "./pages/Settings";
import Templates from "./pages/Templates";
import Tickets from "./pages/Tickets";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth />}>
        <Route path="/inbox" element={<Inbox />} />
        <Route path="/tickets" element={<Tickets />} />
        <Route path="/blocked" element={<BlockedSenders />} />
        <Route path="/templates" element={<Templates />} />
        <Route path="/knowledge" element={<KnowledgeBase />} />
        <Route path="/qa" element={<QAPairs />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/audit" element={<AuditLogs />} />
        <Route path="/conversations/:id" element={<ConversationDetail />} />
      </Route>
      <Route path="*" element={<Navigate to="/inbox" replace />} />
    </Routes>
  );
}
