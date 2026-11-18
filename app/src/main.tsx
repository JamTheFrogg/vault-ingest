import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { Popup } from "./Popup";
import { SetupAiScreen } from "./AiEngine";
import { isPopup } from "./shell";
import "./styles.css";

const popup = isPopup();
// ?view=setup-ai: the AI step of first-run setup on its own, until the onboarding flow embeds it
const setup = new URLSearchParams(location.search).get("view") === "setup-ai";
document.documentElement.classList.toggle("popup-window", popup);
createRoot(document.getElementById("root")!).render(<StrictMode>{popup ? <Popup /> : setup ? <SetupAiScreen /> : <App />}</StrictMode>);
