/*
 * App — The Agent Brief
 *
 * Daily Dispatch design system: light by default, dark theme is switchable from the header.
 * Routes:
 *   /                — Home (Today's brief, recent briefs, latest episode)
 *   /brief/:id       — Single daily brief detail
 *   /archive         — Filterable list of all past briefs
 *   /podcast         — Featured episode + episode grid
 *   /about           — About the publication
 *   *                — NotFound (on-brand 404)
 */

import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Route, Switch } from "wouter";
import ErrorBoundary from "./components/ErrorBoundary";
import { ThemeProvider } from "./contexts/ThemeContext";

import Home from "./pages/Home";
import BriefDetail from "./pages/BriefDetail";
import Archive from "./pages/Archive";
import Podcast from "./pages/Podcast";
import About from "./pages/About";
import NotFound from "./pages/NotFound";

function Router() {
  return (
    <Switch>
      <Route path="/" component={Home} />
      <Route path="/brief/:id" component={BriefDetail} />
      <Route path="/archive" component={Archive} />
      <Route path="/podcast" component={Podcast} />
      <Route path="/podcast/:id" component={Podcast} />
      <Route path="/about" component={About} />
      <Route path="/404" component={NotFound} />
      <Route component={NotFound} />
    </Switch>
  );
}

function App() {
  return (
    <ErrorBoundary>
      <ThemeProvider defaultTheme="light" switchable>
        <TooltipProvider>
          <Toaster richColors closeButton position="bottom-right" />
          <Router />
        </TooltipProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}

export default App;
