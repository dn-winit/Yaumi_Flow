import React, { useState } from "react";
import { supervisionApi } from "@/api/supervision";
import Loading from "@/components/ui/Loading";
import SessionBrowser from "./SessionBrowser";
import SessionDetail from "./SessionDetail";

export default function ReviewTab() {
  const [selectedSession, setSelectedSession] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSelectSession = async (routeCode: string, date: string) => {
    setLoading(true);
    setError(null);
    try {
      const res = await supervisionApi.loadReview(routeCode, date);
      if (res.exists && res.session) {
        setSelectedSession(res.session);
      } else {
        setError("Session not found.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load session");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-6">
      <SessionBrowser onSelectSession={handleSelectSession} />

      {loading && <Loading message="Loading session details..." />}

      {error && (
        <div className="bg-danger-50 border border-danger-100 rounded-lg p-4 text-body text-danger-700">
          {error}
        </div>
      )}

      {selectedSession && !loading && (
        <SessionDetail session={selectedSession} />
      )}
    </div>
  );
}
