"use client";

/**
 * Loading one piece of server data, with every state a screen must show.
 *
 *   loading  nothing yet
 *   ready    data, possibly `refreshing`, possibly with `refreshError` (a
 *            refresh failed but the earlier data is still on screen, marked
 *            as possibly out of date)
 *   error    the first load failed; there is nothing to show but the error
 *
 * A reply that arrives after a newer request started (or after unmount) is
 * dropped, so a slow response can never overwrite a fresher one.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type Resource<T> =
  | { status: "loading" }
  | { status: "ready"; data: T; refreshing: boolean; refreshError: unknown | null }
  | { status: "error"; error: unknown };

export type Action<T> =
  | { type: "start" }
  | { type: "success"; data: T }
  | { type: "failure"; error: unknown };

export function reduceResource<T>(state: Resource<T>, action: Action<T>): Resource<T> {
  switch (action.type) {
    case "start":
      return state.status === "ready"
        ? { ...state, refreshing: true }
        : state.status === "error"
          ? { status: "loading" }
          : state;
    case "success":
      return { status: "ready", data: action.data, refreshing: false, refreshError: null };
    case "failure":
      return state.status === "ready"
        ? { ...state, refreshing: false, refreshError: action.error }
        : { status: "error", error: action.error };
  }
}

/** Replace the ready data without a request (for example after a confirmed write). */
export function mapReady<T>(state: Resource<T>, change: (data: T) => T): Resource<T> {
  return state.status === "ready" ? { ...state, data: change(state.data) } : state;
}

export interface UseResource<T> {
  state: Resource<T>;
  /** Fetch again. Resolves true on success. Never throws. */
  reload: () => Promise<boolean>;
  /** Change the held data locally, for example after a confirmed write. */
  update: (change: (data: T) => T) => void;
}

export function useResource<T>(load: () => Promise<T>, enabled = true): UseResource<T> {
  const [state, setState] = useState<Resource<T>>({ status: "loading" });
  const latest = useRef(0);
  const loader = useRef(load);
  loader.current = load;
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const reload = useCallback(async () => {
    const ticket = ++latest.current;
    setState((current) => reduceResource(current, { type: "start" }));
    try {
      const data = await loader.current();
      if (!alive.current || ticket !== latest.current) return false;
      setState((current) => reduceResource(current, { type: "success", data }));
      return true;
    } catch (error) {
      if (!alive.current || ticket !== latest.current) return false;
      setState((current) => reduceResource(current, { type: "failure", error }));
      return false;
    }
  }, []);

  useEffect(() => {
    if (enabled) void reload();
  }, [enabled, reload]);

  const update = useCallback((change: (data: T) => T) => {
    setState((current) => mapReady(current, change));
  }, []);

  return { state, reload, update };
}
