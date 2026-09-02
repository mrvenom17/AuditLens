import { serverFetch } from "@/lib/server-api";
import { type UserSummary } from "@/types/api";
import { UserManagement } from "./UserManagement";

export const metadata = { title: "User Management · AuditLens" };

export default async function AdminUsersPage() {
  const users = await serverFetch<UserSummary[]>("/api/admin/users");

  return (
    <>
      <div className="page-head">
        <div>
          <h1>User Management</h1>
          <p className="page-sub">Create and manage access across the firm.</p>
        </div>
      </div>
      <UserManagement initialUsers={users} />
    </>
  );
}
