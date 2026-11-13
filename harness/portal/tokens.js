// src/theme/tokens.js: Portal design tokens (BRIEF-PORTAL-REDESIGN rev2, Phase 1).
// Phase 1 moves every colour literal here WITHOUT changing a pixel: the dark theme below holds the exact values the
// portal used before. The light theme arrives in Phase 2 from the Claude Design canvas "Bifrost Portal Redesign";
// switching ACTIVE_THEME is then the one-line, revertible flip (G1). Dark stays selectable (P6).
export const themes = {
  dark: {
    BG: "#080B11",
    SURF: "#0F1319",
    ELEV: "#151C29",
    BORD: "#1C2538",
    BORDB: "#283050",
    TX: "#D8E2F4",
    TXD: "#68778E",
    TXM: "#323C54",
    GRN: "#3CB87A",
    BLU: "#4E8ED4",
    AMB: "#D09435",
    ORG: "#C47035",
    RSE: "#BC4868",
    PRP: "#8658C8",
    PRESET_ROUTING: "#2ECC71",
    PRESET_AUTOPILOT: "#9B59B6",
    PRESET_PRIVACY: "#E8A838",
    PRESET_RFP: "#E87040",
    TIER_1A_HEARTH: "#3DB87A",
    TIER_1A_INSTRUCT: "#4CB890",
    TIER_2_QWEN_CODER: "#E8A040",
    TIER_2_QWEN_KILO2: "#C8702A",
    TIER_3_GEMINI: "#D05878",
    TIER_3_FAST: "#C84060",
    TIER_3_OPENROUTER: "#C03060",
    MODE_WORKSHOP_OFFLINE: "#6880C0",
    SLATE: "#4E78C4",
    SLATE_LT: "#7898C4",
    WARN_YEL: "#fbbf24",
    WARN_YEL_BG: "rgba(251,191,36,0.15)",
    WARN_YEL_BORDER: "rgba(251,191,36,0.3)",
    LOCAL_CYAN: "#0ea5e9",
    ERR_RED: "#ef4444",
    BLACK: "#000",
    TERM_MATRIX: "#00ff41",
    TERM_GRN: "#33ff66",
    TERM_GRN_GLOW: "rgba(51, 255, 102, 0.2)",
    TERM_GRN_GLOW_STRONG: "rgba(51, 255, 102, 0.25)",
  },
  light: {
    BG: "#FAFBFD",
    SURF: "#F4F6F8",
    ELEV: "#E9EDF4",
    BORD: "#D4DBEA",
    BORDB: "#B9C0DC",
    TX: "#0F1D36",
    TXD: "#566376",
    TXM: "#596B96",
    GRN: "#319764",
    BLU: "#4588D2",
    AMB: "#B07C29",
    ORG: "#C47035",
    RSE: "#BC4868",
    PRP: "#8658C8",
    PRESET_ROUTING: "#239955",
    PRESET_AUTOPILOT: "#9B59B6",
    PRESET_PRIVACY: "#B47A15",
    PRESET_RFP: "#E45821",
    TIER_1A_HEARTH: "#329763",
    TIER_1A_INSTRUCT: "#3B9573",
    TIER_2_QWEN_CODER: "#BD7617",
    TIER_2_QWEN_KILO2: "#C8702A",
    TIER_3_GEMINI: "#D05878",
    TIER_3_FAST: "#C84060",
    TIER_3_OPENROUTER: "#C03060",
    MODE_WORKSHOP_OFFLINE: "#6880C0",
    SLATE: "#4E78C4",
    SLATE_LT: "#6387BB",
    WARN_YEL: "#805D02",
    WARN_YEL_BG: "rgba(214,158,0,0.14)",
    WARN_YEL_BORDER: "rgba(214,158,0,0.35)",
    LOCAL_CYAN: "#0C8EC9",
    ERR_RED: "#ef4444",
    BLACK: "#000",
    TERM_MATRIX: "#00A52A",
    TERM_GRN: "#00A529",
    TERM_GRN_GLOW: "rgba(0, 106, 27, 0.16)",
    TERM_GRN_GLOW_STRONG: "rgba(0, 106, 27, 0.22)",
  },
};

export const ACTIVE_THEME = "light";

const T = themes[ACTIVE_THEME];

export const {
  BG, SURF, ELEV, BORD, BORDB, TX, TXD, TXM, GRN, BLU, AMB, ORG, RSE, PRP,
  PRESET_ROUTING, PRESET_AUTOPILOT, PRESET_PRIVACY, PRESET_RFP,
  TIER_1A_HEARTH, TIER_1A_INSTRUCT, TIER_2_QWEN_CODER, TIER_2_QWEN_KILO2,
  TIER_3_GEMINI, TIER_3_FAST, TIER_3_OPENROUTER,
  MODE_WORKSHOP_OFFLINE, SLATE, SLATE_LT,
  WARN_YEL, WARN_YEL_BG, WARN_YEL_BORDER, LOCAL_CYAN, ERR_RED, BLACK,
  TERM_MATRIX, TERM_GRN, TERM_GRN_GLOW, TERM_GRN_GLOW_STRONG,
} = T;

// ---------------------------------------------------------------------------
// TYPE SCALE (2026-09-29)
// ---------------------------------------------------------------------------
// Every surface sets type as an inline `fontSize: <px>` number -- 321 sites across
// BifrostPortal, CommandSurface, ArchitectFanout and ModeTier. They all go through fs(),
// so this ONE number is the whole UI's type scale.
//
//   1.0  restores the sizes the Portal shipped with before 2026-09-29, exactly.
//   1.5  the dominant 8 / 8.5 / 9 px band becomes 12 / 13 / 13.5 px, which is roughly
//        the Claude desktop app's text size (John's reference: "what the claude desktop
//        app scales is good").
//
// Relative hierarchy is preserved because every size is multiplied by the same factor.
// Rounding is to the nearest 0.5px, matching the half-pixel sizes already in use.
export const TYPE_SCALE = 1.50;
export const fs = (n) => (typeof n === "number" ? Math.round(n * TYPE_SCALE * 2) / 2 : n);
