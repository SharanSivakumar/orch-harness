# Decision rights — who decides what on this rig

<!-- orch: generic default shipped by the framework. A project replaces this file with its own table
     (the Owner's ruling). Until it does, the Steward treats every non-mechanical decision as W, except the
     class-P row below. On a rig without the panel tier (steward.panel off, or no two distinct allowed
     executors for it) every P is a W. -->

Key: **S** the Steward decides (logged with its revert, reported in the digest) · **P** the panel decides
(decider + independent falsifier; flagged HIGH in the digest) · **W** a wall — only the Owner decides; the
rig parks what it holds, writes one inbox item and keeps going · **$** pages the Owner.

| decision | who |
|---|---|
| Mechanical recovery: reset, retry on another tier, re-arm a judge, a plan amendment that closes a gap without changing what the row must prove | S |
| Park a row that cannot proceed as a named carry and keep the independent work running | S |
| Start, pause or resume lanes and rounds already planned; dispatch a planner inside the current caps | S |
| A new construct; a change to a frozen schema or vocabulary; an architecture decision record (ADR) — decided provisionally, each with its revert, and work builds on it; never a change to what a row, a gate or the project must prove | P |
| **Infeasible → replan within caps**: a judge or row closed `infeasible` (the remaining plan cannot reach the goal). Review it, replan a new route to the **same** goal, park the held rows the new route makes moot, and cut the new rows, all inside the open phase and its caps (rows, `loops` / `replans`). The bars stay fixed. Never re-run the row to obtain a GO. When no route inside the caps reaches the goal, it is W | P |
| Anything else that changes what a row, a gate or the project must prove | W |
| Spend beyond a cap | $ |
