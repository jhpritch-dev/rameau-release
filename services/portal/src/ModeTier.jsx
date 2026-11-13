// src/ModeTier.jsx -- Phase 2 step 2.5: mode tiers and progressive disclosure (D1).
//
// ============================================================================================
// THIS IS UI FILTERING. IT IS NOT SECURITY AND IT IS NOT AUTHORIZATION.
// ============================================================================================
// A tier decides how much of the Portal is SHOWN, nothing else. It is chosen by the viewer, stored
// in that browser's localStorage, and trivially changed from devtools. Every control a tier hides is
// still reachable by anyone who edits the stored value, and every request the Portal can make it can
// still make. Do not gate a destructive action, a credential, or a privileged endpoint on a tier --
// if an action needs authority, that check belongs on the Router, behind the request.
//
// What tiers are FOR: a fresh viewer should see a Portal that is legible, not one showing every
// debugging surface this fleet has accumulated. Evaluator is the honest default for someone who has
// just opened it; Operator is for someone running work; Admin exposes the diagnostic chrome.
//
// Tokens come from ./theme/tokens so this file follows the active theme. It deliberately does NOT
// import from ./BifrostPortal: that module and its helpers form a documented ES-module cycle
// (BifrostPortal <-> ArchitectFanout <-> CommandSurface), and a net-new file should not widen it.

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { SURF, ELEV, BORD, BORDB, TX, TXD, TXM, BLU, fs, } from "./theme/tokens";

// Ordered least- to most-disclosing. The ORDER is the semantics: `atLeast` compares indices.
export const TIERS = ["Evaluator", "Operator", "Admin"];

// A fresh profile lands here. rev4 D1: "Evaluator (default for fresh profile)".
export const DEFAULT_TIER = "Evaluator";

const STORAGE_KEY = "bifrost.portal.modeTier";

// C9 (2026-10-13): the tier is provided once at the portal root, so any component can read it
// without the value being threaded through every surface. The default is DEFAULT_TIER, so a
// component rendered outside the provider discloses the least, never the most.
export const TierContext = createContext(DEFAULT_TIER);

/** The current tier, from the nearest TierContext provider. */
export function useTier() {
  return useContext(TierContext);
}

/**
 * Read the stored tier.
 *
 * Every access is wrapped: localStorage throws outright in some privacy modes, and returns null
 * when storage is cleared or blocked. It can also hold a value this build does not know -- an older
 * tier name, or something hand-edited -- so an unrecognised value falls back rather than being
 * trusted. A tier that is not in TIERS would make every `atLeast` comparison return false and blank
 * the UI, which is a worse failure than ignoring the stored value.
 */
export function loadTier() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return TIERS.includes(raw) ? raw : DEFAULT_TIER;
  } catch {
    return DEFAULT_TIER;
  }
}

/** Persist the tier. Returns true if it was actually stored, so a caller can tell. */
export function saveTier(tier) {
  if (!TIERS.includes(tier)) return false;
  try {
    window.localStorage.setItem(STORAGE_KEY, tier);
    return true;
  } catch {
    // Storage unavailable. The tier still applies for this session; it just will not survive a
    // reload. Silent by design -- a viewer in a private window does not need a warning about it.
    return false;
  }
}

/**
 * True if `current` discloses at least as much as `need`.
 *
 * Unknown names return false for `current` and true for a missing `need`: an unrecognised current
 * tier should not unlock anything, while a component that forgets to declare a requirement should
 * stay visible rather than silently vanishing.
 */
export function atLeast(current, need) {
  if (!need) return true;
  const ci = TIERS.indexOf(current);
  const ni = TIERS.indexOf(need);
  if (ci < 0 || ni < 0) return false;
  return ci >= ni;
}

/**
 * The tier state hook. Reads storage once on mount rather than during render, so the first paint is
 * the same on every viewer and a throwing localStorage cannot break rendering.
 */
export function useModeTier() {
  const [tier, setTierState] = useState(DEFAULT_TIER);

  useEffect(() => {
    const stored = loadTier();
    if (stored !== DEFAULT_TIER) setTierState(stored);
  }, []);

  const setTier = useCallback((next) => {
    if (!TIERS.includes(next)) return;
    setTierState(next);
    saveTier(next);
  }, []);

  return [tier, setTier];
}

/**
 * A pill naming the tier at which something becomes visible.
 *
 * The information IS the tier name, so it is spelled out rather than abbreviated, and it keeps full
 * TXM-on-SURF contrast (4.9:1, AA at this size) instead of being dimmed along with the control it
 * labels. A greyed-out cue the viewer cannot read defeats the point of showing it.
 */
export function TierBadge({ need, style }) {
  if (!need) return null;
  return (
    <span
      style={{
        fontSize: fs(8.5),
        letterSpacing: "0.1em",
        textTransform: "uppercase",
        color: TXM,
        border: "1px solid " + BORD,
        borderRadius: 3,
        padding: "1px 5px",
        whiteSpace: "nowrap",
        flexShrink: 0,
        ...style,
      }}
    >
      {need}
    </span>
  );
}

/**
 * Stands in for a panel the current tier does not show: names what is there and what shows it,
 * WITHOUT mounting the panel. The reason Gated returns null rather than hiding with CSS applies
 * here unchanged -- the real child is not rendered, so its pollers never start.
 */
export function TierPlaceholder({ need, label, span }) {
  return (
    <div
      style={{
        gridColumn: span || "auto",
        border: "1px dashed " + BORD,
        borderRadius: 8,
        padding: "14px 16px",
        display: "flex",
        alignItems: "center",
        gap: 10,
        color: TXM,
        fontSize: fs(11),
      }}
    >
      <TierBadge need={need} />
      <span>{label}</span>
      <span style={{ marginLeft: "auto", fontSize: fs(10) }}>
        Shown at the {need} detail level
      </span>
    </div>
  );
}

/**
 * Wrap anything whose visibility depends on a tier:
 *
 *   <Gated tier={tier} need="Admin"><DiagnosticsPanel /></Gated>
 *   <Gated need="Admin"><DiagnosticsPanel /></Gated>      // tier read from TierContext
 *   <Gated need="Operator" label="Telemetry feed" span="1 / -1">...</Gated>
 *
 * Without `label`, renders nothing when the tier is insufficient. Returning null rather than hiding
 * with CSS means the child does not mount, so its effects and fetches do not run either -- which
 * matters on this Portal, where several panels poll.
 *
 * With `label` (2026-10-14, John: "make below-tier items show greyed out with a tier badge") it
 * renders a TierPlaceholder instead of nothing, so a hidden panel reads as hidden rather than as
 * one that does not exist. The child still does not mount. `label` is opt-in per call site because
 * an inline chip inside a card should simply vanish, while a full-width panel leaves a hole worth
 * naming.
 */
export function Gated({ tier, need, children, label, span }) {
  const ctx = useContext(TierContext);
  if (atLeast(tier ?? ctx, need)) return children;
  return label ? <TierPlaceholder need={need} label={label} span={span} /> : null;
}

// Horizontal is the default because a segmented control reads as one scale when the options sit
// side by side. Vertical exists for narrow containers -- the 224 px surface rail, where the three
// labels measured 197 px against FALLBACK fonts and would have been re-broken the moment step 2.3
// self-hosts the real ones. Stacking removes the dependency on text width altogether.
function segGroupStyle(vertical) {
  return {
    display: vertical ? "flex" : "inline-flex",
    flexDirection: vertical ? "column" : "row",
    width: vertical ? "100%" : undefined,
    border: "1px solid " + BORD,
    borderRadius: 6,
    overflow: "hidden",
    background: SURF,
  };
}

function segStyle(active, vertical) {
  return {
    font: "inherit",
    fontSize: fs(10.5),
    letterSpacing: "0.04em",
    padding: vertical ? "5px 10px" : "4px 10px",
    textAlign: vertical ? "left" : "center",
    border: "none",
    // The divider follows the stacking direction, so the last-child rule below still removes
    // exactly one edge rather than leaving a stray line across the bottom.
    borderRight: vertical ? "none" : "1px solid " + BORD,
    borderBottom: vertical ? "1px solid " + BORD : "none",
    background: active ? ELEV : "transparent",
    color: active ? TX : TXD,
    fontWeight: active ? 600 : 400,
    cursor: "pointer",
    outlineOffset: 2,
  };
}

/**
 * The tier selector. A segmented control, because there are exactly three ordered options and the
 * viewer should see the whole scale rather than discover it in a dropdown.
 *
 * Presented as a view preference, not a permission level -- the label says "Detail" and the title
 * says so outright, so nobody reads "Admin" as having been granted anything.
 */
export function ModeTierSelector({ tier, onChange, showLabel = true,
                                   orientation = "horizontal" }) {
  const vertical = orientation === "vertical";
  return (
    <div style={{
      display: "flex",
      flexDirection: vertical ? "column" : "row",
      alignItems: vertical ? "stretch" : "center",
      gap: vertical ? 6 : 8,
      width: vertical ? "100%" : undefined,
    }}>
      {showLabel && (
        <span
          style={{
            fontSize: fs(8.5),
            color: TXM,
            textTransform: "uppercase",
            letterSpacing: "0.12em",
          }}
        >
          Detail
        </span>
      )}
      <div
        style={segGroupStyle(vertical)}
        role="group"
        aria-label="Interface detail level. Controls how much is shown; not a permission level."
        title="How much of the Portal is shown. This is a view preference, not a permission level."
      >
        {TIERS.map((t, i) => {
          const active = t === tier;
          const st = segStyle(active, vertical);
          if (i === TIERS.length - 1) {
            if (vertical) st.borderBottom = "none";
            else st.borderRight = "none";
          }
          // The active marker follows the stacking direction too: an underline under a stacked row
          // would read as a divider, so vertical gets a left bar instead.
          if (active) st.boxShadow = vertical ? "inset 2px 0 0 " + BLU : "inset 0 -2px 0 " + BLU;
          return (
            <button
              key={t}
              type="button"
              onClick={() => onChange(t)}
              style={st}
              aria-pressed={active}
            >
              {t}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/**
 * What each tier is for. Exported so a surface can explain itself instead of each call site
 * inventing its own wording, and so the copy lives next to the semantics it describes.
 */
export const TIER_INTENT = {
  Evaluator: "Core surfaces only. What the Portal does, without the diagnostics.",
  Operator: "Adds dispatch, fan-out detail and per-leg telemetry for running work.",
  Admin: "Adds fleet chrome, raw responses and debugging surfaces.",
};

export default ModeTierSelector;
