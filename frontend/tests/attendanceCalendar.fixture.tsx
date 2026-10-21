import React from "react";
import { createRoot } from "react-dom/client";
import { ConfigProvider, theme } from "antd";
import { BrowserRouter } from "react-router-dom";
import { AuthProvider } from "../src/context/AuthContext";
import AttendanceCalendar from "../src/pages/AttendanceCalendar";
import "../src/index.css";
const dark = new URLSearchParams(location.search).has("dark");
document.documentElement.dataset.theme = dark ? "dark" : "light";
createRoot(document.getElementById("root")!).render(
  <ConfigProvider
    theme={{ algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm }}
  >
    <AuthProvider>
      <BrowserRouter>
        <div style={{ padding: 16 }}>
          <AttendanceCalendar />
        </div>
      </BrowserRouter>
    </AuthProvider>
  </ConfigProvider>,
);
