/*
 * NotFound — Daily Dispatch design system
 *
 * The single mascot moment for this page is the waving shrimp,
 * presented in the same calm editorial voice as the rest of the site.
 */

import { Link } from "wouter";
import { Button } from "@/components/ui/button";
import { ArrowLeft, Home } from "lucide-react";
import SiteLayout from "@/components/SiteLayout";
import { BRAND } from "@/lib/brand";

export default function NotFound() {
  return (
    <SiteLayout>
      <section className="container py-20 md:py-28">
        <div className="mx-auto grid max-w-3xl gap-10 md:grid-cols-[auto_1fr] md:items-center">
          <img
            src={BRAND.mascot.waving}
            alt=""
            className="h-40 w-40 object-contain md:h-52 md:w-52"
          />
          <div>
            <p className="kicker-coral">Error 404 · Off the kelp map</p>
            <h1
              className="display-headline mt-3 text-4xl md:text-5xl"
              style={{ fontVariationSettings: '"opsz" 144' }}
            >
              That brief washed away.
            </h1>
            <p className="prose-body mt-4">
              We couldn&rsquo;t find the page you were looking for. It may have moved, or it may
              never have existed in the first place. Either way, our shrimp says hi.
            </p>
            <div className="mt-7 flex flex-wrap gap-3">
              <Link href="/">
                <Button className="rounded-full bg-foreground text-background hover:bg-foreground/90">
                  <Home className="mr-1 h-4 w-4" />
                  Today&rsquo;s brief
                </Button>
              </Link>
              <Link href="/archive">
                <Button variant="outline" className="rounded-full">
                  <ArrowLeft className="mr-1 h-4 w-4" />
                  Browse archive
                </Button>
              </Link>
            </div>
          </div>
        </div>
      </section>
    </SiteLayout>
  );
}
