"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { ROLE_LABELS, type Role, type UserSummary, type AdminUserCreate, type AdminUserUpdate } from "@/types/api";
import "./users.css";

export function UserManagement({ initialUsers }: { initialUsers: UserSummary[] }) {
  const router = useRouter();
  const [users, setUsers] = useState<UserSummary[]>(initialUsers);
  
  // Form State
  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("auditor");
  const [password, setPassword] = useState("");
  
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleCreateUser = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const payload: AdminUserCreate = { name, email, role, password };
      const newUser = await api.post<UserSummary>("/api/admin/users", payload);
      setUsers([...users, newUser]);
      setShowForm(false);
      setName("");
      setEmail("");
      setPassword("");
      setRole("auditor");
      router.refresh();
    } catch (err: unknown) {
      if (err instanceof ApiError) {
        setError(err.displayMessage || err.message || "Failed to create user.");
      } else {
        setError("An unexpected error occurred.");
      }
    } finally {
      setLoading(false);
    }
  };

  const toggleStatus = async (user: UserSummary) => {
    try {
      const payload: AdminUserUpdate = { is_active: !user.is_active };
      const updated = await api.patch<UserSummary>(`/api/admin/users/${user.id}`, payload);
      setUsers(users.map(u => u.id === user.id ? updated : u));
    } catch (err: unknown) {
      alert("Failed to update user status.");
    }
  };

  return (
    <div className="users-container">
      <div className="users-actions">
        {!showForm && (
          <button className="btn btn-primary" onClick={() => setShowForm(true)}>
            + Create New User
          </button>
        )}
      </div>

      {showForm && (
        <div className="panel new-user-form">
          <h3>Create New User</h3>
          <form onSubmit={handleCreateUser}>
            {error && <div className="alert alert-error">{error}</div>}
            
            <div className="form-group">
              <label>Name</label>
              <input type="text" value={name} onChange={e => setName(e.target.value)} required />
            </div>
            
            <div className="form-group">
              <label>Email</label>
              <input type="email" value={email} onChange={e => setEmail(e.target.value)} required />
            </div>
            
            <div className="form-group">
              <label>Role</label>
              <select value={role} onChange={e => setRole(e.target.value as Role)} required>
                <option value="auditor">Auditor (can perform audits)</option>
                <option value="reviewer">Reviewer (can review and finalize)</option>
                <option value="admin">Admin (manage users and corpus)</option>
              </select>
            </div>
            
            <div className="form-group">
              <label>Password (min 12 chars)</label>
              <input type="password" value={password} onChange={e => setPassword(e.target.value)} required minLength={12} />
            </div>

            <div className="form-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setShowForm(false)} disabled={loading}>
                Cancel
              </button>
              <button type="submit" className="btn btn-primary" disabled={loading}>
                {loading ? "Creating..." : "Create User"}
              </button>
            </div>
          </form>
        </div>
      )}

      <div className="panel">
        {users.length === 0 ? (
          <div className="empty">
            <p>No users found.</p>
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Role</th>
                <th>Status</th>
                <th style={{ textAlign: "right" }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map(u => (
                <tr key={u.id}>
                  <td><strong>{u.name}</strong></td>
                  <td className="mono muted">{u.email}</td>
                  <td><span className="pill pill-neutral">{ROLE_LABELS[u.role]}</span></td>
                  <td>
                    <span className={u.is_active ? "pill pill-satisfied" : "pill pill-error"}>
                      {u.is_active ? "Active" : "Inactive"}
                    </span>
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <button 
                      className="btn btn-sm btn-ghost" 
                      onClick={() => toggleStatus(u)}
                    >
                      {u.is_active ? "Deactivate" : "Activate"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
