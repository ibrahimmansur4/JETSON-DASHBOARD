# =============================================================
#  auth.py  —  Dashboard password authentication
#
#  Single shared password for the whole team.  The bcrypt hash
#  is stored in config.py (DASHBOARD_PASSWORD_HASH).
#
#  Generate a hash:   python auth.py set-password
#  Then paste it into config.py.
# =============================================================

import sys

import bcrypt
import streamlit as st

import config as cfg


def hash_password(plain):
    """Generate a bcrypt hash from a plaintext password."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(plain, hashed):
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def render_login_gate():
    """
    Show a login form if the user is not authenticated.
    Returns True if authenticated, False otherwise.

    If DASHBOARD_PASSWORD_HASH is empty, authentication is disabled
    and all users are allowed through.
    """
    pw_hash = cfg.DASHBOARD_PASSWORD_HASH

    # No password configured — skip authentication
    if not pw_hash:
        return True

    if st.session_state.get("authenticated"):
        return True

    st.markdown("# Jetson Dashboard")
    st.markdown("---")
    st.markdown("### Login Required")

    with st.form("login_form"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", use_container_width=True)

    if submitted:
        if check_password(password, pw_hash):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    return False


# ── CLI: generate a password hash ────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "set-password":
        import getpass
        pw = getpass.getpass("Enter dashboard password: ")
        pw2 = getpass.getpass("Confirm password: ")
        if pw != pw2:
            print("Passwords do not match.")
            sys.exit(1)
        h = hash_password(pw)
        print(f"\nPaste this into config.py as DASHBOARD_PASSWORD_HASH:\n")
        print(f'DASHBOARD_PASSWORD_HASH = "{h}"')
    else:
        print("Usage: python auth.py set-password")
