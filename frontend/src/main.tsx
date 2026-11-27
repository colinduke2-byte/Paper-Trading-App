import { createRoot } from "react-dom/client";
import App from "./App";
import { AccessGate } from "./Login";
import { RecoveryGate } from "./DataBackups";
import "./styles.css";

createRoot(document.getElementById("root")!).render(<AccessGate><RecoveryGate><App /></RecoveryGate></AccessGate>);
