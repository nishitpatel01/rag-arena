import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./index.css";
import { App } from "./app/App";
import { ThemeProvider } from "./theme/ThemeProvider";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root DOM element '#root' not found in index.html");
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <ThemeProvider>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ThemeProvider>
  </React.StrictMode>
);
