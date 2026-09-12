"""Airbyte jobs API helpers (public API /v1/jobs)."""

from __future__ import annotations

import httpx

from dataplat.services._http import raise_for_status


def list_jobs(
    client: httpx.Client,
    base_url: str,
    *,
    connection_id: str | None = None,
    status: str | None = None,
    job_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List jobs, newest first."""
    params: dict = {
        "limit": limit,
        "orderBy": "createdAt|DESC",
    }
    if connection_id:
        params["connectionId"] = connection_id
    if status:
        params["status"] = status
    if job_type:
        params["jobType"] = job_type

    response = client.get(f"{base_url}/api/public/v1/jobs", params=params)
    raise_for_status(response, "list jobs")
    payload = response.json() or {}
    data = payload.get("data") or []
    return data if isinstance(data, list) else []


def get_job(client: httpx.Client, base_url: str, job_id: str) -> dict:
    response = client.get(f"{base_url}/api/public/v1/jobs/{job_id}")
    raise_for_status(response, "get job")
    return response.json()


def cancel_job(client: httpx.Client, base_url: str, job_id: str) -> dict:
    response = client.delete(f"{base_url}/api/public/v1/jobs/{job_id}")
    raise_for_status(response, "cancel job")
    return response.json() if response.text else {}


def trigger_job(
    client: httpx.Client,
    base_url: str,
    connection_id: str,
    job_type: str,
) -> dict:
    """Trigger a job (jobType: sync, reset, refresh, or clear)."""
    response = client.post(
        f"{base_url}/api/public/v1/jobs",
        json={"connectionId": connection_id, "jobType": job_type},
    )
    raise_for_status(response, f"trigger {job_type} job")
    return response.json()
