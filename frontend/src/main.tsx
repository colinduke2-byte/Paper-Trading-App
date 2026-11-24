import { createRoot } from "react-dom/client";
import App from "./App";
import { AccessGate } from "./Login";
import "./styles.css";
import { installDialogKeyboard } from "./dialogs";

installDialogKeyboard();

createRoot(document.getElementById("root")!).render(<AccessGate><App /></AccessGate>);
