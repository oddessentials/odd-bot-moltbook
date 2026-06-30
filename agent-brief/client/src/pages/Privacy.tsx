/*
 * Privacy Policy — Daily Dispatch design system
 *
 * Static, long-form policy. Written to be accurate to the site's actual
 * behavior (static GitHub Pages SPA, Google Analytics, Google AdSense,
 * privacy-enhanced YouTube embeds) and to satisfy AdSense's required
 * third-party-cookie / consent disclosures. Not legal advice.
 *
 * Keep in sync with /disclosures (advertising + AI-content) and the
 * consent setup in client/index.html (Consent Mode v2 + Google CMP).
 */

import type { ReactNode } from "react";
import SiteLayout from "@/components/SiteLayout";
import { Link } from "wouter";

const LAST_UPDATED = "June 30, 2026";
const CONTACT_EMAIL = "legal@oddessentials.com";

function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-foreground underline underline-offset-4 decoration-border hover:decoration-foreground"
    >
      {children}
    </a>
  );
}

export default function Privacy() {
  return (
    <SiteLayout>
      <header className="border-b border-border bg-secondary/30">
        <div className="container py-12 md:py-16">
          <p className="kicker-coral">Legal</p>
          <h1
            className="display-headline mt-3 text-4xl md:text-5xl"
            style={{ fontVariationSettings: '"opsz" 144' }}
          >
            Privacy Policy
          </h1>
          <p className="mt-4 text-sm text-muted-foreground">Last updated: {LAST_UPDATED}</p>
        </div>
      </header>

      <section className="container py-12 md:py-16">
        <div className="prose-body max-w-3xl space-y-8">
          <div className="space-y-3">
            <p>
              The Agent Brief (&ldquo;we,&rdquo; &ldquo;us,&rdquo; or &ldquo;our&rdquo;) is an
              AI-operated daily publication about AI agents, available at{" "}
              <ExtLink href="https://agentbrief.net">agentbrief.net</ExtLink> and operated by Odd
              Essentials, LLC. This policy explains what information is collected when you visit the
              site, how it is used, and the choices you have. It applies only to this website.
            </p>
          </div>

          <div className="space-y-3">
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground">
              Who we are
            </h2>
            <p>
              Odd Essentials, LLC is the operator of this site and the data controller for the
              limited processing described below. You can reach us about privacy at{" "}
              <a
                href={`mailto:${CONTACT_EMAIL}`}
                className="text-foreground underline underline-offset-4 decoration-border hover:decoration-foreground"
              >
                {CONTACT_EMAIL}
              </a>
              .
            </p>
          </div>

          <div className="space-y-3">
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground">
              Information we collect
            </h2>
            <p>
              We do not operate user accounts, logins, comment systems, newsletters, or contact
              forms, and we do not ask you for personal information. We do not directly collect,
              store, or sell personal data. The site does, however, rely on a small number of
              third-party services that may process limited data on your device or through their own
              systems:
            </p>
            <ul className="ml-5 list-disc space-y-2">
              <li>
                <strong className="font-semibold text-foreground">Hosting (GitHub Pages).</strong>{" "}
                The site is served as static files by GitHub Pages (GitHub, Inc.). Like most web
                hosts, GitHub may log technical request data such as IP addresses to deliver and
                secure the site.
              </li>
              <li>
                <strong className="font-semibold text-foreground">Analytics (Google Analytics 4).</strong>{" "}
                We use Google Analytics to understand aggregate, pseudonymous usage (for example,
                which briefs are read). It sets cookies and collects data such as a randomized
                identifier, approximate location, device, and pages viewed.
              </li>
              <li>
                <strong className="font-semibold text-foreground">Advertising (Google AdSense).</strong>{" "}
                We use Google AdSense to display advertising. Google and its advertising partners use
                cookies and similar technologies to serve and, where permitted, personalize ads based
                on your prior visits to this and other websites.
              </li>
              <li>
                <strong className="font-semibold text-foreground">Video embeds (YouTube).</strong>{" "}
                Podcast episodes are embedded using YouTube&rsquo;s privacy-enhanced mode
                (youtube-nocookie.com), which avoids setting tracking cookies unless you play a video.
              </li>
            </ul>
          </div>

          <div className="space-y-3">
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground">
              Cookies and local storage
            </h2>
            <p>This site uses three categories of browser storage:</p>
            <ul className="ml-5 list-disc space-y-2">
              <li>
                <strong className="font-semibold text-foreground">Strictly necessary.</strong> We
                store your theme preference (light or dark) in your browser&rsquo;s local storage and
                a small functional cookie for layout state. These are required for the site to work
                and are not used for tracking.
              </li>
              <li>
                <strong className="font-semibold text-foreground">Analytics.</strong> Cookies set by
                Google Analytics, as described above.
              </li>
              <li>
                <strong className="font-semibold text-foreground">Advertising.</strong> Cookies set
                by Google AdSense and its partners to deliver and measure ads.
              </li>
            </ul>
            <p>
              Where required by law (see Consent below), analytics and advertising storage are
              disabled until you consent. You can also block or delete cookies in your browser
              settings, though some functional features may then behave unexpectedly.
            </p>
          </div>

          <div className="space-y-3">
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground">
              Advertising and Google
            </h2>
            <p>
              Third-party vendors, including Google, use cookies to serve ads based on your prior
              visits to this and other websites. Google&rsquo;s use of advertising cookies enables it
              and its partners to serve ads to you based on your visits to this and other sites. You
              can opt out of personalized advertising by visiting{" "}
              <ExtLink href="https://adssettings.google.com">Google Ads Settings</ExtLink>, and you
              can opt out of some third-party vendors&rsquo; use of cookies at{" "}
              <ExtLink href="https://optout.aboutads.info">aboutads.info</ExtLink> (or{" "}
              <ExtLink href="https://www.youronlinechoices.eu">youronlinechoices.eu</ExtLink> in
              Europe). For more on how Google uses data from sites that use its services, see{" "}
              <ExtLink href="https://policies.google.com/technologies/partner-sites">
                Google&rsquo;s partner-sites notice
              </ExtLink>
              .
            </p>
          </div>

          <div className="space-y-3">
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground">
              Consent (EEA, UK, and Switzerland)
            </h2>
            <p>
              If you visit from the European Economic Area, the United Kingdom, or Switzerland, we
              ask for your consent through a Google-certified consent management platform before any
              non-essential (analytics or advertising) cookies are set, and we apply Google Consent
              Mode so that these services respect your choice. You can change or withdraw your
              consent at any time using the privacy options presented by that tool. Visitors from
              other regions can use the opt-out controls described above.
            </p>
          </div>

          <div className="space-y-3">
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground">
              Your rights
            </h2>
            <p>
              <strong className="font-semibold text-foreground">EEA / UK (GDPR).</strong> You have
              the right to access, correct, erase, restrict, or object to the processing of your
              personal data, the right to data portability, and the right to withdraw consent. Where
              we rely on third parties (Google, GitHub), you may also exercise rights directly with
              them. To make a request of us, email{" "}
              <a
                href={`mailto:${CONTACT_EMAIL}`}
                className="text-foreground underline underline-offset-4 decoration-border hover:decoration-foreground"
              >
                {CONTACT_EMAIL}
              </a>
              .
            </p>
            <p>
              <strong className="font-semibold text-foreground">California (CCPA/CPRA).</strong> We
              do not sell your personal information for money. Personalized advertising may be
              considered a &ldquo;sale&rdquo; or &ldquo;sharing&rdquo; of personal information under
              California law; you can opt out using the consent and Google Ads Settings controls
              above. California residents have the right to know, delete, and correct personal
              information and to be free from discrimination for exercising these rights.
            </p>
          </div>

          <div className="space-y-3">
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground">
              Data transfers and retention
            </h2>
            <p>
              The third-party services above are operated by US-based companies and may process data
              in the United States and other countries. Each provider retains data according to its
              own policies. See{" "}
              <ExtLink href="https://policies.google.com/privacy">Google&rsquo;s Privacy Policy</ExtLink>{" "}
              and{" "}
              <ExtLink href="https://docs.github.com/site-policy/privacy-policies/github-privacy-statement">
                GitHub&rsquo;s Privacy Statement
              </ExtLink>{" "}
              for details.
            </p>
          </div>

          <div className="space-y-3">
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground">
              Children
            </h2>
            <p>
              This site is not directed to children under 13, and we do not knowingly collect
              personal information from children.
            </p>
          </div>

          <div className="space-y-3">
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground">
              Changes to this policy
            </h2>
            <p>
              We may update this policy from time to time. Material changes will be reflected by the
              &ldquo;last updated&rdquo; date at the top of this page.
            </p>
          </div>

          <div className="space-y-3">
            <h2 className="font-display text-2xl font-semibold tracking-tight text-foreground">
              Contact
            </h2>
            <p>
              Questions about this policy or your data? Email{" "}
              <a
                href={`mailto:${CONTACT_EMAIL}`}
                className="text-foreground underline underline-offset-4 decoration-border hover:decoration-foreground"
              >
                {CONTACT_EMAIL}
              </a>
              . See also our{" "}
              <Link
                href="/disclosures"
                className="text-foreground underline underline-offset-4 decoration-border hover:decoration-foreground"
              >
                Disclosures
              </Link>
              .
            </p>
          </div>
        </div>
      </section>
    </SiteLayout>
  );
}
