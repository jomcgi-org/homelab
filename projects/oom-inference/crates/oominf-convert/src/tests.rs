use std::collections::BTreeMap;
use std::path::Path;

use oominf_format::Model;
use serde_json::json;

use super::*;

/// Writes one safetensors shard from (name, dtype, shape, bytes).
fn write_shard(path: &Path, tensors: &[(String, &str, Vec<u64>, Vec<u8>)]) {
    let mut header = serde_json::Map::new();
    let mut data = Vec::new();
    for (name, dtype, shape, bytes) in tensors {
        let start = data.len();
        data.extend_from_slice(bytes);
        header.insert(
            name.clone(),
            json!({"dtype": dtype, "shape": shape, "data_offsets": [start, data.len()]}),
        );
    }
    let h = serde_json::to_vec(&header).unwrap();
    let mut out = (h.len() as u64).to_le_bytes().to_vec();
    out.extend_from_slice(&h);
    out.extend_from_slice(&data);
    std::fs::write(path, out).unwrap();
}

/// A tiny two-layer, two-expert qwen4_exp checkpoint split over two shards.
fn tiny_checkpoint(dir: &Path) -> BTreeMap<String, Vec<u8>> {
    let mut all = BTreeMap::new();
    let mut experts = Vec::new();
    let mut other = Vec::new();
    let mut seed = 1u8;
    let mut next = |n: usize| {
        seed = seed.wrapping_mul(31).wrapping_add(7);
        (0..n)
            .map(|i| seed.wrapping_add(i as u8))
            .collect::<Vec<u8>>()
    };
    for layer in 0..2 {
        for e in 0..2 {
            for (proj, w, s) in [
                ("gate", [4, 8], [4, 1]),
                ("up", [4, 8], [4, 1]),
                ("down", [16, 2], [16, 1]),
            ] {
                let p = format!("model.language_model.layers.{layer}.mlp.experts.{e}.{proj}_proj");
                experts.push((
                    format!("{p}.weight"),
                    "U8",
                    w.to_vec(),
                    next(w[0] as usize * w[1] as usize),
                ));
                experts.push((
                    format!("{p}.weight_scale"),
                    "F8_E4M3",
                    s.to_vec(),
                    next(s[0] as usize),
                ));
                experts.push((format!("{p}.weight_scale_2"), "F32", vec![], next(4)));
                experts.push((format!("{p}.input_scale"), "F32", vec![], next(4)));
            }
        }
    }
    other.push(("lm_head.weight".to_owned(), "BF16", vec![3, 4], next(24)));
    other.push((
        "model.language_model.layers.10.mlp.gate.weight".to_owned(),
        "BF16",
        vec![2, 4],
        next(16),
    ));
    other.push((
        "model.language_model.layers.2.mlp.gate.weight".to_owned(),
        "BF16",
        vec![2, 4],
        next(16),
    ));
    other.push((
        "model.language_model.layers.1.ple.ple_embedding.ngram_embedding.shard_0.weight".to_owned(),
        "F8_E4M3",
        vec![5, 3],
        next(15),
    ));
    // Stacked MTP experts: [experts, 2 * inter, hidden] and [experts, hidden, inter].
    other.push((
        "mtp.layers.0.mlp.experts.gate_up_proj".to_owned(),
        "BF16",
        vec![2, 8, 8],
        next(256),
    ));
    other.push((
        "mtp.layers.0.mlp.experts.down_proj".to_owned(),
        "BF16",
        vec![2, 8, 4],
        next(128),
    ));
    other.push((
        "model.visual.patch_embed.proj.weight".to_owned(),
        "BF16",
        vec![2],
        next(4),
    ));

    write_shard(&dir.join("a.safetensors"), &experts);
    write_shard(&dir.join("b.safetensors"), &other);
    let mut map = serde_json::Map::new();
    for (n, ..) in &experts {
        map.insert(n.clone(), json!("a.safetensors"));
    }
    for (n, ..) in &other {
        map.insert(n.clone(), json!("b.safetensors"));
    }
    std::fs::write(
        dir.join("model.safetensors.index.json"),
        json!({"weight_map": map}).to_string(),
    )
    .unwrap();
    std::fs::write(
        dir.join("config.json"),
        json!({"model_type": "qwen4_exp", "text_config": {"num_hidden_layers": 2, "num_experts": 2}}).to_string(),
    )
    .unwrap();
    for (n, _, _, b) in experts.into_iter().chain(other) {
        all.insert(n, b);
    }
    all
}

#[test]
fn converts_and_reads_back_every_byte() {
    let src = tempfile::tempdir().unwrap();
    let out = tempfile::tempdir().unwrap();
    let source = tiny_checkpoint(src.path());
    let (index, summary) = convert(
        src.path(),
        &out.path().join("m.oom"),
        &Options::default(),
        |_| {},
    )
    .unwrap();
    assert_eq!(
        (
            summary.dense,
            summary.tables,
            summary.expert_layers,
            summary.skipped
        ),
        (3, 1, 3, 1)
    );

    let names: Vec<_> = index.tensors.iter().map(|t| t.name.as_str()).collect();
    assert_eq!(
        names,
        [
            "lm_head.weight",
            "model.language_model.layers.2.mlp.gate.weight",
            "model.language_model.layers.10.mlp.gate.weight",
            "model.language_model.layers.1.ple.ple_embedding.ngram_embedding.shard_0.weight",
        ]
    );

    let m = Model::open(&out.path().join("m.oom")).unwrap();
    for t in &m.index().tensors {
        assert_eq!(m.read_tensor(t).unwrap(), source[&t.name], "{}", t.name);
    }
    for layer in 0..2 {
        let g = m.expert_group(layer).unwrap().clone();
        let mut rec = vec![0u8; g.schema.stride as usize];
        for e in 0..2 {
            m.read_record(layer, e, &mut rec).unwrap();
            let base = format!("model.language_model.layers.{layer}.mlp.experts.{e}");
            let mut scalars = Vec::new();
            for proj in ["gate", "up", "down"] {
                for field in ["weight", "weight_scale"] {
                    let p = g.schema.part(&format!("{proj}.{field}")).unwrap();
                    let got = &rec[p.offset as usize..(p.offset + p.nbytes) as usize];
                    assert_eq!(got, &source[&format!("{base}.{proj}_proj.{field}")][..]);
                }
                for field in ["weight_scale_2", "input_scale"] {
                    scalars.extend_from_slice(&source[&format!("{base}.{proj}_proj.{field}")]);
                }
            }
            assert_eq!(&rec[..24], &scalars[..]);
        }
    }
    // The MTP group (after the 2 decoder layers): bf16 slices of the stacked tensors.
    let g = m.expert_group(2).unwrap().clone();
    assert_eq!(g.schema.layout, "bf16");
    let mut rec = vec![0u8; g.schema.stride as usize];
    let (gu, dn) = (
        &source["mtp.layers.0.mlp.experts.gate_up_proj"],
        &source["mtp.layers.0.mlp.experts.down_proj"],
    );
    for e in 0..2usize {
        m.read_record(2, e as u32, &mut rec).unwrap();
        let part = |n: &str| {
            let p = g.schema.part(n).unwrap();
            rec[p.offset as usize..(p.offset + p.nbytes) as usize].to_vec()
        };
        assert_eq!(part("gate.weight"), gu[e * 128..e * 128 + 64]);
        assert_eq!(part("up.weight"), gu[e * 128 + 64..(e + 1) * 128]);
        assert_eq!(part("down.weight"), dn[e * 64..(e + 1) * 64]);
    }
    assert!(m.verify(|_| {}).unwrap().mismatches.is_empty());
    assert!(out.path().join("m.oom/config.json").exists());
}

#[test]
fn layer_filter_and_no_tables() {
    let src = tempfile::tempdir().unwrap();
    let out = tempfile::tempdir().unwrap();
    tiny_checkpoint(src.path());
    let opts = Options {
        expert_layers: Some(1..=1),
        tables: false,
    };
    let (index, summary) = convert(src.path(), out.path(), &opts, |_| {}).unwrap();
    assert_eq!(
        index
            .expert_groups
            .iter()
            .map(|g| g.layer)
            .collect::<Vec<_>>(),
        [1]
    );
    assert_eq!(summary.excluded, 24 + 2 + 1);
    assert_eq!(index.files.tables.bytes, 0);
}

#[test]
fn rejects_incomplete_expert_sets() {
    let src = tempfile::tempdir().unwrap();
    tiny_checkpoint(src.path());
    let path = src.path().join("model.safetensors.index.json");
    let mut idx: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    idx["weight_map"]
        .as_object_mut()
        .unwrap()
        .remove("model.language_model.layers.0.mlp.experts.1.up_proj.input_scale");
    std::fs::write(&path, idx.to_string()).unwrap();
    let out = tempfile::tempdir().unwrap();
    let err = convert(src.path(), out.path(), &Options::default(), |_| {}).unwrap_err();
    assert!(err.to_string().contains("layer 0"), "{err}");
}

#[test]
fn natural_order() {
    let mut v = vec!["l.10.a", "l.2.a", "l.02.b", "l.1.z", "a"];
    v.sort_by(|a, b| natural_cmp(a, b));
    assert_eq!(v, ["a", "l.1.z", "l.2.a", "l.02.b", "l.10.a"]);
}
