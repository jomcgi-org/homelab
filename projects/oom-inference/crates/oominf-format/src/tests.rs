use super::*;
use crate::writer::put_part;

fn schema() -> RecordSchema {
    RecordSchema::new(
        "test-layout",
        &[
            ("scalars", "F32", &[3]),
            ("w", "U8", &[300]),
            ("s", "BF16", &[10]),
        ],
    )
    .unwrap()
}

#[test]
fn schema_aligns_parts_and_stride() {
    let s = schema();
    let offsets: Vec<_> = s.parts.iter().map(|p| (p.offset, p.nbytes)).collect();
    assert_eq!(offsets, vec![(0, 12), (256, 300), (768, 20)]);
    assert_eq!(s.stride, ALIGN);
}

#[test]
fn qwen_record_geometry_matches_format_doc() {
    let s = RecordSchema::new(
        "nvfp4-modelopt-g16",
        &[
            ("scalars", "F32", &[6]),
            ("gate.weight", "U8", &[640, 1280]),
            ("gate.weight_scale", "F8_E4M3", &[640, 160]),
            ("up.weight", "U8", &[640, 1280]),
            ("up.weight_scale", "F8_E4M3", &[640, 160]),
            ("down.weight", "U8", &[2560, 320]),
            ("down.weight_scale", "F8_E4M3", &[2560, 40]),
        ],
    )
    .unwrap();
    assert_eq!(s.stride, 2_768_896);
}

#[test]
fn round_trip_and_verify() {
    let dir = tempfile::tempdir().unwrap();
    let src = Source {
        path: "mem".into(),
        model_type: "test".into(),
        fingerprint: String::new(),
    };
    let mut w = Writer::create(dir.path(), src).unwrap();
    let dense: Vec<u8> = (0..10u8).collect();
    w.add_tensor(TensorFile::Dense, "a", "BF16", &[5], &dense)
        .unwrap();
    let table = vec![7u8; 5000];
    w.add_tensor(TensorFile::Tables, "t", "U8", &[50, 100], &table)
        .unwrap();
    let s = schema();
    w.add_expert_group(3, 4, s.clone(), |e, rec| {
        put_part(&s, rec, "w", &vec![e as u8 + 1; 300])?;
        put_part(&s, rec, "scalars", &[e as u8; 12])
    })
    .unwrap();
    let index = w.finish().unwrap();
    assert_eq!(index.files.dense.bytes, ALIGN);
    assert_eq!(index.files.tables.bytes, 2 * ALIGN);
    assert_eq!(index.files.experts.bytes, 4 * ALIGN);

    let m = Model::open(dir.path()).unwrap();
    assert_eq!(m.read_tensor(m.tensor("a").unwrap()).unwrap(), dense);
    assert_eq!(m.read_tensor(m.tensor("t").unwrap()).unwrap(), table);
    let mut rec = vec![0u8; ALIGN as usize];
    m.read_record(3, 2, &mut rec).unwrap();
    assert_eq!(&rec[256..556], &[3u8; 300][..]);
    assert_eq!(&rec[..12], &[2u8; 12][..]);
    assert!(rec[556..].iter().all(|&b| b == 0));
    assert!(m.read_record(3, 4, &mut rec).is_err());
    assert!(m.read_record(0, 0, &mut rec).is_err());

    let report = m.verify(|_| {}).unwrap();
    assert_eq!((report.tensors_checked, report.records_checked), (2, 4));
    assert!(report.mismatches.is_empty());
}

#[test]
fn verify_detects_corruption() {
    let dir = tempfile::tempdir().unwrap();
    let src = Source {
        path: "mem".into(),
        model_type: "test".into(),
        fingerprint: String::new(),
    };
    let mut w = Writer::create(dir.path(), src).unwrap();
    let s = schema();
    w.add_expert_group(0, 2, s.clone(), |_, rec| put_part(&s, rec, "w", &[1; 300]))
        .unwrap();
    w.finish().unwrap();

    use std::os::unix::fs::FileExt;
    let f = std::fs::OpenOptions::new()
        .write(true)
        .open(dir.path().join(EXPERTS_FILE))
        .unwrap();
    f.write_all_at(&[0xff], ALIGN + 4000).unwrap(); // padding byte of record 1
    let report = Model::open(dir.path()).unwrap().verify(|_| {}).unwrap();
    assert_eq!(report.mismatches, vec!["layer 0 expert 1".to_owned()]);
}

#[test]
fn refuses_existing_conversion_and_size_mismatch() {
    let dir = tempfile::tempdir().unwrap();
    let src = Source {
        path: "mem".into(),
        model_type: "test".into(),
        fingerprint: String::new(),
    };
    let mut w = Writer::create(dir.path(), src.clone()).unwrap();
    assert!(
        w.add_tensor(TensorFile::Dense, "bad", "F32", &[3], &[0; 8])
            .is_err()
    );
    w.finish().unwrap();
    assert!(Writer::create(dir.path(), src).is_err());
}
