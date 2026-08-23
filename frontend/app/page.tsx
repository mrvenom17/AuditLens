import { redirect } from "next/navigation";

export default function Home() {
  // There is no anonymous landing page. This is internal firm software with no
  // public surface (05_SECURITY.md §10.8), so the root is just a door: the
  // authenticated layout sends you to /login if you have no session.
  redirect("/engagements");
}
