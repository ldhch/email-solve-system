import { Navigate, Route, Routes } from "react-router-dom";
import { RequireAuth } from "./components/AuthGuard";
import ConversationDetail from "./pages/ConversationDetail";
import Inbox from "./pages/Inbox";
import Login from "./pages/Login";
import Tickets from "./pages/Tickets";

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth />}>
        <Route path="/inbox" element={<Inbox />} />
        <Route path="/tickets" element={<Tickets />} />
        <Route path="/conversations/:id" element={<ConversationDetail />} />
      </Route>
      <Route path="*" element={<Navigate to="/inbox" replace />} />
    </Routes>
  );
}
