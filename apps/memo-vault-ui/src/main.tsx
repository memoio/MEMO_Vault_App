import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import OwnerSessionGate from "./OwnerSessionGate";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <OwnerSessionGate><App /></OwnerSessionGate>
  </React.StrictMode>,
);
