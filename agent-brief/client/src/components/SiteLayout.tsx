/*
 * SiteLayout — Daily Dispatch design system
 *
 * Persistent top nav + footer wrapper. Every page renders inside this so users
 * always have an obvious escape route from any subpage.
 */

import { useEffect } from "react";
import { useLocation } from "wouter";
import SiteHeader from "./SiteHeader";
import SiteFooter from "./SiteFooter";

interface SiteLayoutProps {
  children: React.ReactNode;
}

export function SiteLayout({ children }: SiteLayoutProps) {
  const [location] = useLocation();

  // Scroll to top on route change so subpages don't inherit Home's scroll position.
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
  }, [location]);

  return (
    <div className="flex min-h-screen flex-col">
      <SiteHeader />
      <main className="flex-1">{children}</main>
      <SiteFooter />
    </div>
  );
}

export default SiteLayout;
