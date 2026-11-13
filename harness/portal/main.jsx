import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import BifrostPortal from "./BifrostPortal.jsx"
createRoot(document.getElementById("root")).render(
  <StrictMode>
    <BifrostPortal />
  </StrictMode>
)
