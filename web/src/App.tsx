import React, { Suspense } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Spinner } from "./components/ui";
import { useAuth } from "./lib/context";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import Drafts from "./pages/Drafts";
import Publications from "./pages/Publications";
import Sources from "./pages/Sources";
import Channels from "./pages/Channels";

const Trends = React.lazy(() => import("./pages/Trends"));
const ProjectSettings = React.lazy(() => import("./pages/ProjectSettings"));
const Admin = React.lazy(() => import("./pages/Admin"));

function Protected({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <div className="p-10"><Spinner /></div>;
  if (!user) return <Navigate to="/login" state={{ from: loc.pathname }} replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Suspense fallback={<div className="p-10"><Spinner /></div>}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<Protected><Layout /></Protected>}>
          <Route index element={<Overview />} />
          <Route path="trends" element={<Trends />} />
          <Route path="drafts/:id?" element={<Drafts />} />
          <Route path="publications" element={<Publications />} />
          <Route path="sources" element={<Sources />} />
          <Route path="channels" element={<Channels />} />
          <Route path="project" element={<ProjectSettings />} />
          <Route path="admin" element={<Admin />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
