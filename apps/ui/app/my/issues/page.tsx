import { redirect } from "next/navigation"

// The three "My …" views merged into one tabbed /my page (issue #283). Kept as a redirect
// so existing links and bookmarks still land on the right tab.
export default function MyIssuesRedirect() {
  redirect("/my?tab=issues")
}
