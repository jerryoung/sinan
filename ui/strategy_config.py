"""策略配置页:表单化(参数带 ❓ 说明、枚举下拉)/ 高级 YAML 双模式。"""
import hashlib

import streamlit as st
import yaml

from sinan.config import (StrategyCfg, load_live_profiles,
                          resolve_live_profile)
from ui.common import ROOT, current_sel, render_param_form, to_yaml
from ui.theme import page_header


def page():
    sel_bt, _, sel_label = current_sel()
    page_header("策略配置", f"{sel_label} · {sel_bt.name} · 参数、标的池与实盘配置引用",
                eyebrow="Strategy definition")
    raw = sel_bt.read_text(encoding="utf-8")
    d0 = yaml.safe_load(raw)
    # 控件 key 绑定文件内容指纹:YAML 在别处(高级模式/外部编辑)变更后,
    # 表单必须按新内容重建,否则旧控件状态会在下次"保存"时把参数悄悄改回去
    fkey = f"{sel_bt.stem}_{hashlib.md5(raw.encode()).hexdigest()[:8]}"
    mode = st.radio("配置方式", ["表单", "高级(YAML)"], horizontal=True,
                    key=f"mode_{sel_bt.stem}")

    if mode == "表单":
        edited = render_param_form(d0, prefix=fkey)
        text = to_yaml(edited)
        with st.expander("生成的 YAML 预览"):
            st.code(text, language="yaml")
    else:
        if st.session_state.get("_editing_file") != fkey:
            st.session_state["_editing_file"] = fkey
            st.session_state["yaml_text"] = raw
        text = st.text_area("YAML(可直接编辑;运行回测使用此处内容)",
                            key="yaml_text", height=280)

    def _parse(txt) -> StrategyCfg:
        cfg = StrategyCfg(**yaml.safe_load(txt))
        resolve_live_profile(load_live_profiles(), cfg)
        return cfg

    e1, e2, e3, _ = st.columns([1, 1, 2, 3])
    if e1.button("校验"):
        try:
            c = _parse(text)
            st.success(f"合法:{c.name} / {c.strategy} / {len(c.universe)} 标的")
        except Exception as ex:                    # noqa: BLE001 展示给用户
            st.error(f"解析失败:{ex}")
    if e2.button("保存"):
        try:
            _parse(text)
            sel_bt.write_text(text, encoding="utf-8")
            st.success(f"已保存 {sel_bt.name}")
        except Exception as ex:                    # noqa: BLE001
            st.error(f"未保存,YAML 不合法:{ex}")
    new_name = e3.text_input("另存为", label_visibility="collapsed",
                             placeholder="另存为:输入新文件名(不含 .yaml)")
    if new_name and st.button(f"另存为 {new_name}.yaml"):
        try:
            _parse(text)
            fp = ROOT / "config" / "strategies" / f"{new_name}.yaml"
            fp.write_text(text, encoding="utf-8")
            st.success(f"已保存 {fp.name},刷新页面后出现在下拉框")
        except Exception as ex:                    # noqa: BLE001
            st.error(f"未保存:{ex}")
