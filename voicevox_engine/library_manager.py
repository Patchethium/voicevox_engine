import base64
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException
from pydantic import ValidationError
from semver.version import Version

from voicevox_engine.model import (
    DownloadableLibraryInfo,
    InstalledLibraryInfo,
    VvlibManifest,
)

__all__ = ["LibraryManager"]

INFO_FILE = "metas.json"


class LibraryManager:
    """音声ライブラリ (`.vvlib`) の管理"""

    def __init__(
        self,
        library_root_dir: Path,
        supported_vvlib_version: str | None,
        brand_name: str,
        engine_name: str,
        engine_uuid: str,
    ):
        self.library_root_dir = library_root_dir
        self.library_root_dir.mkdir(exist_ok=True)
        if supported_vvlib_version is not None:
            self.supported_vvlib_version = Version.parse(supported_vvlib_version)
        else:
            # supported_vvlib_versionがNoneの時は0.0.0として扱う
            self.supported_vvlib_version = Version.parse("0.0.0")
        self.engine_brand_name = brand_name
        self.engine_name = engine_name
        self.engine_uuid = engine_uuid

    def downloadable_libraries(self) -> list[DownloadableLibraryInfo]:
        """
        ダウンロード可能ライブラリの一覧を取得
        Returns
        -------
        - : list[DownloadableLibraryInfo]
        """
        # == ダウンロード情報をネットワーク上から取得する場合
        # url = "https://example.com/downloadable_libraries.json"
        # response = requests.get(url)
        # return list(map(DownloadableLibrary.parse_obj, response.json()))

        # == ダウンロード情報をjsonファイルから取得する場合
        # with open(
        #     self.root_dir / "engine_manifest_assets" / "downloadable_libraries.json",
        #     encoding="utf-8",
        # ) as f:
        #     return list(map(DownloadableLibrary.parse_obj, json.load(f)))

        # ダミーとして、speaker_infoのアセットを読み込む
        with open(
            "./engine_manifest_assets/downloadable_libraries.json",
            encoding="utf-8",
        ) as f:
            libraries = json.load(f)
            speaker_info = libraries[0]["speakers"][0]["speaker_info"]
            mock_root_dir = Path("./speaker_info/7ffcb7ce-00ec-4bdc-82cd-45a8889e43ff")
            speaker_info["policy"] = (mock_root_dir / "policy.md").read_text()
            speaker_info["portrait"] = base64.b64encode(
                (mock_root_dir / "portrait.png").read_bytes()
            )
            for style_info in speaker_info["style_infos"]:
                style_id = style_info["id"]
                style_info["icon"] = base64.b64encode(
                    (mock_root_dir / "icons" / f"{style_id}.png").read_bytes()
                )
                style_info["voice_samples"] = [
                    base64.b64encode(
                        (
                            mock_root_dir / "voice_samples" / f"{style_id}_{i:0>3}.wav"
                        ).read_bytes()
                    )
                    for i in range(1, 4)
                ]
            return list(map(DownloadableLibraryInfo.parse_obj, libraries))

    def installed_libraries(self) -> dict[str, InstalledLibraryInfo]:
        """
        インストール済み音声ライブラリの情報を取得
        Returns
        -------
        library : Dict[str, InstalledLibraryInfo]
            インストール済みライブラリの情報
        """
        library: dict[str, InstalledLibraryInfo] = {}
        for library_dir in self.library_root_dir.iterdir():
            if library_dir.is_dir():
                # ライブラリ情報の取得 from `library_root_dir / f"{library_uuid}" / "metas.json"`
                library_uuid = os.path.basename(library_dir)
                with open(library_dir / INFO_FILE, encoding="utf-8") as f:
                    info = json.load(f)
                # アンインストール出来ないライブラリを作る場合、何かしらの条件でFalseを設定する
                library[library_uuid] = InstalledLibraryInfo(**info, uninstallable=True)
        return library

    def install_library(self, library_id: str, file: BinaryIO) -> Path:
        """
        音声ライブラリ (`.vvlib`) のインストール
        Parameters
        ----------
        library_id : str
            インストール対象ライブラリID
        file : BytesIO
            ライブラリファイルBlob
        Returns
        -------
        library_dir : Path
            インストール済みライブラリの情報
        """
        for downloadable_library in self.downloadable_libraries():
            if downloadable_library.uuid == library_id:
                library_info = downloadable_library.dict()
                break
        else:
            raise HTTPException(
                status_code=404, detail=f"指定された音声ライブラリ {library_id} が見つかりません。"
            )

        # ライブラリディレクトリの生成
        library_dir = self.library_root_dir / library_id
        library_dir.mkdir(exist_ok=True)

        # metas.jsonの生成
        with open(library_dir / INFO_FILE, "w", encoding="utf-8") as f:
            json.dump(library_info, f, indent=4, ensure_ascii=False)

        # zipファイル形式のバリデーション
        if not zipfile.is_zipfile(file):
            raise HTTPException(
                status_code=422, detail=f"音声ライブラリ {library_id} は不正なファイルです。"
            )

        with zipfile.ZipFile(file) as zf:
            if zf.testzip() is not None:
                raise HTTPException(
                    status_code=422, detail=f"音声ライブラリ {library_id} は不正なファイルです。"
                )

            # マニフェストファイルの存在とファイル形式をバリデーション
            vvlib_manifest = None
            try:
                vvlib_manifest = json.loads(
                    zf.read("vvlib_manifest.json").decode("utf-8")
                )
            except KeyError:
                raise HTTPException(
                    status_code=422,
                    detail=f"指定された音声ライブラリ {library_id} にvvlib_manifest.jsonが存在しません。",
                )
            except Exception:
                raise HTTPException(
                    status_code=422,
                    detail=f"指定された音声ライブラリ {library_id} のvvlib_manifest.jsonは不正です。",
                )

            # マニフェスト形式のバリデーション
            try:
                VvlibManifest.validate(vvlib_manifest)
            except ValidationError:
                raise HTTPException(
                    status_code=422,
                    detail=f"指定された音声ライブラリ {library_id} のvvlib_manifest.jsonに不正なデータが含まれています。",
                )

            # ライブラリバージョンのバリデーション
            if not Version.is_valid(vvlib_manifest["version"]):
                raise HTTPException(
                    status_code=422, detail=f"指定された音声ライブラリ {library_id} のversionが不正です。"
                )

            # マニフェストバージョンのバリデーション
            try:
                vvlib_manifest_version = Version.parse(
                    vvlib_manifest["manifest_version"]
                )
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"指定された音声ライブラリ {library_id} のmanifest_versionが不正です。",
                )
            if vvlib_manifest_version > self.supported_vvlib_version:
                raise HTTPException(
                    status_code=422, detail=f"指定された音声ライブラリ {library_id} は未対応です。"
                )

            # ライブラリ-エンジン対応のバリデーション
            if vvlib_manifest["engine_uuid"] != self.engine_uuid:
                raise HTTPException(
                    status_code=422,
                    detail=f"指定された音声ライブラリ {library_id} は{self.engine_name}向けではありません。",
                )

            # 展開によるインストール
            zf.extractall(library_dir)

        return library_dir

    def uninstall_library(self, library_id: str) -> None:
        """
        インストール済み音声ライブラリのアンインストール
        Parameters
        ----------
        library_id : str
            インストール対象ライブラリID
        """
        # 対象ライブラリがインストール済みであることの確認
        installed_libraries = self.installed_libraries()
        if library_id not in installed_libraries.keys():
            raise HTTPException(
                status_code=404, detail=f"指定された音声ライブラリ {library_id} はインストールされていません。"
            )

        # アンインストール許可フラグのバリデーション
        if not installed_libraries[library_id].uninstallable:
            raise HTTPException(
                status_code=403, detail=f"指定された音声ライブラリ {library_id} はアンインストールできません。"
            )

        # ディレクトリ削除によるアンインストール
        try:
            shutil.rmtree(self.library_root_dir / library_id)
        except Exception:
            raise HTTPException(
                status_code=500, detail=f"指定された音声ライブラリ {library_id} の削除に失敗しました。"
            )
