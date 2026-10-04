"""
Production artifact：版本化、自足（self-contained）、原子寫入、可回滾
------------------------------------------------------------
目錄（預設 pipeline/artifacts/production/，可用環境變數 MODEL_ARTIFACT_DIR 改）：

  versions/<artifact_version>/bundle.joblib    模型、特徵順序、scaler（在 sklearn pipeline 內）、收縮參數、補值常數、
                                               傷病校準狀態、訓練 metadata（全部推論需要的東西）
  versions/<artifact_version>/manifest.json    metadata + bundle 的 sha256 + 上線前檢查結果（gates）
  CURRENT.json                                 目前 production 指向哪個版本（os.replace 原子替換）
  history.jsonl                                promote / rollback 紀錄（只追加）
  .lock                                        重訓 / promote / rollback 的互斥鎖（fcntl）

原則
  * 版本目錄寫入時先寫到 versions/.tmp-<uuid>/，全部 fsync 後才 rename 成正式名稱；中途失敗只會留下 .tmp-*（會被忽略、
    下次重訓清掉），CURRENT 完全不受影響。
  * 版本目錄一旦建立就不再修改；**永不自動刪除**舊版本（rollback 需要）。
  * 推論只讀 CURRENT → 版本目錄，不需要鎖：CURRENT 是原子替換，版本目錄不可變。
  * 載入時版本不相容（schema / 特徵版本 / scikit-learn 版本 / 檔案雜湊 / 缺欄位）→ IncompatibleArtifactError，不會默默繼續。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import joblib
import sklearn

from ..models import temporal_features as tf
from ..timeutil import now_utc
from . import spec

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "artifacts" / "production"
BUNDLE = "bundle.joblib"
MANIFEST = "manifest.json"
REQUIRED_BUNDLE_KEYS = {"schema_version", "model_version", "artifact_version", "feature_version",
                        "training_cutoff_utc", "training_seasons", "trained_at_utc", "spec", "profiles", "metadata",
                        "library_versions"}
REQUIRED_PROFILE_KEYS = {"timing", "blend_params", "fills", "calibrator", "models"}


class ArtifactError(RuntimeError):
    pass


class IncompatibleArtifactError(ArtifactError):
    pass


class NoProductionArtifact(ArtifactError):
    pass


class LockBusy(ArtifactError):
    pass


def artifact_root(root: str | Path | None = None) -> Path:
    return Path(root or os.environ.get("MODEL_ARTIFACT_DIR") or DEFAULT_ROOT)


def library_versions() -> dict[str, str]:
    import numpy
    import pandas
    import platform
    return {"sklearn": sklearn.__version__, "numpy": numpy.__version__, "pandas": pandas.__version__,
            "joblib": joblib.__version__, "python": platform.python_version()}


def _minor(v: str) -> str:
    return ".".join(v.split(".")[:2])


def make_artifact_version(cutoff: datetime, trained_at: datetime) -> str:
    return f"{spec.MODEL_VERSION}+{cutoff:%Y%m%dT%H%MZ}.{trained_at:%Y%m%dT%H%M%SZ}"


# ------------------------------------------------------------------ #
# 檔案工具                                                              #
# ------------------------------------------------------------------ #

def _fsync_file(path: Path) -> None:
    with open(path, "rb") as f:
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_json(path: Path, obj: Any) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    _fsync_file(tmp)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


@contextlib.contextmanager
def exclusive_lock(root: Path, *, blocking: bool = False) -> Iterator[None]:
    """重訓 / promote / rollback 互斥（同一台主機的多個行程）。拿不到鎖 → LockBusy（不等待）。"""
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(root / ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as e:
            raise LockBusy("另一個重訓 / promote / rollback 正在進行") from e
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ------------------------------------------------------------------ #
# 寫入                                                                  #
# ------------------------------------------------------------------ #

def save_version(bundle: dict[str, Any], root: str | Path | None = None, *, gates: dict | None = None,
                 verify: Callable[[dict], None] | None = None, _fail_after_bundle: bool = False) -> Path:
    """原子寫入一個版本目錄（不改 CURRENT）。回傳版本目錄。
    verify：對「從暫存目錄重新載入」的 bundle 做檢查（例如預測一致性），失敗則整個版本不落地。
    _fail_after_bundle 只供測試模擬中途失敗。"""
    root = artifact_root(root)
    validate_bundle(bundle)
    versions = root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    final = versions / bundle["artifact_version"]
    if final.exists():
        raise ArtifactError(f"版本 {bundle['artifact_version']} 已存在（版本目錄不可覆寫）")
    tmp = versions / f".tmp-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        joblib.dump(bundle, tmp / BUNDLE, compress=3)
        _fsync_file(tmp / BUNDLE)
        if _fail_after_bundle:
            raise ArtifactError("模擬：寫完 bundle 後失敗")
        reloaded = joblib.load(tmp / BUNDLE)
        validate_bundle(reloaded)
        if verify is not None:
            verify(reloaded)
        manifest = manifest_of(bundle, sha256=_sha256(tmp / BUNDLE), gates=gates)
        (tmp / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1, default=str))
        _fsync_file(tmp / MANIFEST)
        _fsync_dir(tmp)
        os.rename(tmp, final)
        _fsync_dir(versions)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final


def manifest_of(bundle: dict[str, Any], *, sha256: str, gates: dict | None) -> dict[str, Any]:
    return {k: bundle[k] for k in ("schema_version", "model_version", "artifact_version", "feature_version",
                                   "training_cutoff_utc", "training_seasons", "trained_at_utc", "library_versions",
                                   "spec")} | {
        "sha256": sha256, "bundle_file": BUNDLE, "gates": gates or {},
        "known_good": bool(gates and gates.get("passed")),
        "metadata": bundle["metadata"], "profiles": {p: {"timing": v["timing"]} for p, v in bundle["profiles"].items()}}


def cleanup_tmp(root: str | Path | None = None) -> int:
    """清掉中斷留下的 .tmp-*（只在持有 exclusive_lock 時呼叫）。"""
    versions = artifact_root(root) / "versions"
    n = 0
    if versions.exists():
        for p in versions.glob(".tmp-*"):
            shutil.rmtree(p, ignore_errors=True)
            n += 1
    return n


def _append_history(root: Path, event: dict[str, Any]) -> None:
    with open(root / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def promote(version: str, root: str | Path | None = None, *, reason: str = "retrain") -> dict[str, Any]:
    """CURRENT → version（原子替換）。版本必須能完整載入且通過 gates（known_good）。呼叫端應持有 exclusive_lock。"""
    root = artifact_root(root)
    art = load_version(version, root)
    if not art.manifest.get("known_good"):
        raise ArtifactError(f"{version} 未通過上線前檢查，不可 promote")
    prev = current_version(root)
    pointer = {"artifact_version": version, "promoted_at": now_utc().isoformat(), "previous": prev, "reason": reason}
    _atomic_write_json(root / "CURRENT.json", pointer)
    _append_history(root, {"event": "promote", **pointer})
    return pointer


def rollback(root: str | Path | None = None, *, to: str | None = None) -> dict[str, Any]:
    """CURRENT 退回上一個曾上線的 known-good 版本（或指定版本）。不刪除任何版本。"""
    root = artifact_root(root)
    cur = current_version(root)
    if to is None:
        promoted = [e["artifact_version"] for e in read_history(root) if e.get("event") in ("promote", "rollback")]
        candidates = [v for v in reversed(promoted) if v != cur]
        seen: set[str] = set()
        to = None
        for v in candidates:
            if v in seen:
                continue
            seen.add(v)
            if (root / "versions" / v).exists():
                to = v
                break
        if to is None:
            raise ArtifactError("沒有可回滾的上一個版本")
    art = load_version(to, root)
    if not art.manifest.get("known_good"):
        raise ArtifactError(f"{to} 不是 known-good，不可回滾到它")
    pointer = {"artifact_version": to, "promoted_at": now_utc().isoformat(), "previous": cur, "reason": "rollback"}
    _atomic_write_json(root / "CURRENT.json", pointer)
    _append_history(root, {"event": "rollback", **pointer})
    return pointer


# ------------------------------------------------------------------ #
# 讀取 / 驗證                                                            #
# ------------------------------------------------------------------ #

@dataclass
class LoadedArtifact:
    path: Path
    manifest: dict[str, Any]
    bundle: dict[str, Any]

    @property
    def artifact_version(self) -> str:
        return self.bundle["artifact_version"]

    @property
    def model_version(self) -> str:
        return self.bundle["model_version"]

    @property
    def training_cutoff_utc(self) -> str:
        return self.bundle["training_cutoff_utc"]

    def profile(self, name: str) -> dict[str, Any]:
        return self.bundle["profiles"][name]

    def calibrator(self, name: str) -> tf.InjuryCalibrator:
        return tf.InjuryCalibrator.from_state(self.profile(name)["calibrator"], frozen=True)


def read_history(root: str | Path | None = None) -> list[dict[str, Any]]:
    p = artifact_root(root) / "history.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def current_version(root: str | Path | None = None) -> str | None:
    p = artifact_root(root) / "CURRENT.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())["artifact_version"]


def list_versions(root: str | Path | None = None) -> list[dict[str, Any]]:
    root = artifact_root(root)
    cur = current_version(root)
    out = []
    for d in sorted((root / "versions").glob("*")) if (root / "versions").exists() else []:
        if d.name.startswith(".") or not (d / MANIFEST).exists():
            continue
        m = json.loads((d / MANIFEST).read_text())
        out.append({"artifact_version": d.name, "current": d.name == cur, "known_good": m.get("known_good"),
                    "training_cutoff_utc": m.get("training_cutoff_utc"), "trained_at_utc": m.get("trained_at_utc"),
                    "n_train": m.get("metadata", {}).get("n_train")})
    return out


def validate_bundle(bundle: dict[str, Any]) -> None:
    missing = REQUIRED_BUNDLE_KEYS - set(bundle)
    if missing:
        raise IncompatibleArtifactError(f"artifact 缺少欄位 {sorted(missing)}")
    if bundle["schema_version"] not in spec.SUPPORTED_SCHEMA_VERSIONS:
        raise IncompatibleArtifactError(
            f"artifact schema_version={bundle['schema_version']}，程式只支援 {sorted(spec.SUPPORTED_SCHEMA_VERSIONS)}")
    if bundle["feature_version"] != tf.FEATURE_VERSION:
        raise IncompatibleArtifactError(
            f"artifact 特徵版本 {bundle['feature_version']} ≠ 程式 {tf.FEATURE_VERSION}（需重新訓練）")
    if bundle["model_version"] != spec.MODEL_VERSION:
        raise IncompatibleArtifactError(f"artifact model_version {bundle['model_version']} ≠ 程式 {spec.MODEL_VERSION}")
    lib = bundle["library_versions"].get("sklearn", "")
    if _minor(lib) != _minor(sklearn.__version__):
        raise IncompatibleArtifactError(
            f"artifact 以 scikit-learn {lib} 訓練，目前是 {sklearn.__version__}（pickle 不保證跨版本相容，需重新訓練）")
    for name in spec.PROFILES:
        prof = bundle["profiles"].get(name)
        if prof is None:
            raise IncompatibleArtifactError(f"artifact 缺少 profile {name}")
        miss = REQUIRED_PROFILE_KEYS - set(prof)
        if miss:
            raise IncompatibleArtifactError(f"profile {name} 缺少 {sorted(miss)}")
        for target in spec.PRODUCTION_SPEC:
            m = prof["models"].get(target)
            if not m or not m.get("features") or m.get("estimator") is None:
                raise IncompatibleArtifactError(f"profile {name} 缺少 {target} 模型或特徵清單")
            n_in = getattr(m["estimator"], "n_features_in_", None)
            if n_in is not None and n_in != len(m["features"]):
                raise IncompatibleArtifactError(f"{name}/{target}：模型輸入維度 {n_in} ≠ 特徵清單 {len(m['features'])}")
        try:
            tf.InjuryCalibrator.from_state(prof["calibrator"])
        except (KeyError, ValueError) as e:
            raise IncompatibleArtifactError(f"profile {name} 的傷病校準狀態不相容：{e}") from e


def load_version(version: str, root: str | Path | None = None) -> LoadedArtifact:
    root = artifact_root(root)
    d = root / "versions" / version
    if not (d / MANIFEST).exists() or not (d / BUNDLE).exists():
        raise ArtifactError(f"找不到 artifact 版本 {version}（{d}）")
    manifest = json.loads((d / MANIFEST).read_text())
    if manifest.get("schema_version") not in spec.SUPPORTED_SCHEMA_VERSIONS:
        raise IncompatibleArtifactError(f"manifest schema_version={manifest.get('schema_version')} 不支援")
    if _sha256(d / BUNDLE) != manifest.get("sha256"):
        raise IncompatibleArtifactError(f"{version} 的 bundle 雜湊與 manifest 不符（檔案損毀或被改動）")
    if _minor(manifest.get("library_versions", {}).get("sklearn", "")) != _minor(sklearn.__version__):
        # 在 unpickle 之前就擋下（不同版本的 sklearn 物件 unpickle 可能直接失敗或行為改變）
        raise IncompatibleArtifactError(
            f"artifact 以 scikit-learn {manifest.get('library_versions', {}).get('sklearn')} 訓練，"
            f"目前是 {sklearn.__version__}（需重新訓練）")
    bundle = joblib.load(d / BUNDLE)
    validate_bundle(bundle)
    if bundle["artifact_version"] != version:
        raise IncompatibleArtifactError(f"目錄 {version} 內的 artifact_version 是 {bundle['artifact_version']}")
    return LoadedArtifact(d, manifest, bundle)


def load_current(root: str | Path | None = None) -> LoadedArtifact:
    v = current_version(root)
    if v is None:
        raise NoProductionArtifact(f"{artifact_root(root)} 沒有 CURRENT.json：請先執行 python run_retrain.py")
    return load_version(v, root)
