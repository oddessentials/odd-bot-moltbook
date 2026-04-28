/*
 * SiteHeader — Daily Dispatch design system
 *
 * Layout: brand lockup (mascot + wordmark) on the left, nav links + theme toggle on the right.
 * Mobile: nav collapses into a sheet behind a small menu button.
 * Active link gets a coral underline; otherwise the chrome is intentionally quiet.
 */

import { BRAND } from "@/lib/brand";
import { useTheme } from "@/contexts/ThemeContext";
import { Link, useLocation } from "wouter";
import { useState } from "react";
import { Moon, Sun, Menu, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetTrigger,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";

const NAV_LINKS = [
  { href: "/", label: "Today" },
  { href: "/archive", label: "Archive" },
  { href: "/podcast", label: "Podcast" },
  { href: "/about", label: "About" },
];

function isActive(currentPath: string, target: string): boolean {
  if (target === "/") return currentPath === "/";
  return currentPath === target || currentPath.startsWith(target + "/");
}

function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  if (!toggleTheme) return null;
  return (
    <button
      onClick={toggleTheme}
      aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
      className="inline-flex h-9 w-9 items-center justify-center rounded-full border border-border text-foreground/70 transition-colors hover:text-foreground hover:bg-muted"
    >
      {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
    </button>
  );
}

function BrandLockup() {
  return (
    <Link href="/" className="group inline-flex items-center gap-2.5">
      <span className="relative inline-flex h-9 w-9 items-center justify-center">
        <img
          src={BRAND.mascot.waving}
          alt=""
          className="h-9 w-9 object-contain transition-transform duration-300 group-hover:rotate-[-6deg]"
        />
      </span>
      <span className="flex items-baseline gap-1.5">
        <span
          className="font-display text-[1.35rem] font-semibold leading-none tracking-tight text-foreground"
          style={{ fontVariationSettings: '"opsz" 144' }}
        >
          The Agent Brief
        </span>
      </span>
    </Link>
  );
}

export function SiteHeader() {
  const [location] = useLocation();
  const [open, setOpen] = useState(false);

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/85 backdrop-blur">
      <div className="container flex h-16 items-center justify-between gap-4">
        <BrandLockup />

        {/* Desktop nav */}
        <nav className="hidden md:flex items-center gap-1">
          {NAV_LINKS.map((link) => {
            const active = isActive(location, link.href);
            return (
              <Link
                key={link.href}
                href={link.href}
                className="relative px-3 py-2 text-sm font-medium text-foreground/75 transition-colors hover:text-foreground"
              >
                <span>{link.label}</span>
                <span
                  className="absolute left-3 right-3 -bottom-px h-[2px] rounded-full bg-coral transition-opacity"
                  style={{ opacity: active ? 1 : 0 }}
                  aria-hidden
                />
              </Link>
            );
          })}
        </nav>

        <div className="flex items-center gap-2">
          <ThemeToggle />
          {/* Mobile menu trigger */}
          <Sheet open={open} onOpenChange={setOpen}>
            <SheetTrigger asChild>
              <Button
                variant="outline"
                size="icon"
                className="md:hidden h-9 w-9 rounded-full"
                aria-label="Open navigation"
              >
                <Menu className="h-4 w-4" />
              </Button>
            </SheetTrigger>
            <SheetContent side="right" className="w-72">
              <SheetHeader className="text-left">
                <SheetTitle className="font-display text-xl">{BRAND.name}</SheetTitle>
              </SheetHeader>
              <nav className="mt-6 flex flex-col">
                {NAV_LINKS.map((link) => {
                  const active = isActive(location, link.href);
                  return (
                    <Link
                      key={link.href}
                      href={link.href}
                      onClick={() => setOpen(false)}
                      className="flex items-center justify-between border-b border-border py-3 text-base font-medium text-foreground/85 hover:text-foreground"
                    >
                      <span>{link.label}</span>
                      {active ? <span className="coral-dot" aria-hidden /> : <X className="h-4 w-4 opacity-0" />}
                    </Link>
                  );
                })}
              </nav>
            </SheetContent>
          </Sheet>
        </div>
      </div>
    </header>
  );
}

export default SiteHeader;
