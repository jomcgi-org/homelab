package oci

import (
	"archive/tar"
	"bytes"
	"fmt"
	"io"
	"path"

	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/empty"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/stream"
	"github.com/google/go-containerregistry/pkg/v1/tarball"
	"github.com/google/go-containerregistry/pkg/v1/types"
)

// BuildIndex creates a multi-platform OCI image index with the given layers.
// The config layer should contain small metadata files, weight layers contain
// model weight shards. Annotations are added to the image index manifest.
func BuildIndex(configLayer v1.Layer, weightLayers []v1.Layer, annotations map[string]string) (v1.ImageIndex, error) {
	img, err := buildImage(configLayer, weightLayers)
	if err != nil {
		return nil, fmt.Errorf("building image: %w", err)
	}

	// Single manifest in the index: model weights are architecture-independent
	// data files, so we don't need per-platform child images. Using a single
	// child also avoids a go-containerregistry bug where streaming layers
	// (stream.NewLayer) shared across multiple child images cause
	// "stream was already consumed" errors during push.
	idx := mutate.AppendManifests(empty.Index, mutate.IndexAddendum{
		Add: img,
		Descriptor: v1.Descriptor{
			Platform: &v1.Platform{OS: "linux", Architecture: "amd64"},
		},
	})
	idx = mutate.IndexMediaType(idx, types.OCIImageIndex)

	if len(annotations) > 0 {
		// mutate.Annotations returns partial.WithRawManifest but the underlying
		// type implements v1.ImageIndex when passed an ImageIndex.
		annotated := mutate.Annotations(idx, annotations)
		if ai, ok := annotated.(v1.ImageIndex); ok {
			idx = ai
		}
	}

	return idx, nil
}

// buildImage creates a single-platform OCI image with the given layers.
func buildImage(configLayer v1.Layer, weightLayers []v1.Layer) (v1.Image, error) {
	img := empty.Image

	var layers []mutate.Addendum

	if configLayer != nil {
		layers = append(layers, mutate.Addendum{Layer: configLayer})
	}
	for _, wl := range weightLayers {
		layers = append(layers, mutate.Addendum{Layer: wl})
	}

	var err error
	img, err = mutate.Append(img, layers...)
	if err != nil {
		return nil, fmt.Errorf("appending layers: %w", err)
	}

	return img, nil
}

// ConfigLayer creates an in-memory tar layer from small config files.
// Each file entry has path {modelDir}/{filename} with mode 0644.
func ConfigLayer(files map[string][]byte, modelDir string) (v1.Layer, error) {
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)

	for filename, data := range files {
		hdr := &tar.Header{
			Name: path.Join(modelDir, filename),
			Mode: 0o644,
			Size: int64(len(data)),
		}
		if err := tw.WriteHeader(hdr); err != nil {
			return nil, fmt.Errorf("writing tar header for %s: %w", filename, err)
		}
		if _, err := tw.Write(data); err != nil {
			return nil, fmt.Errorf("writing tar data for %s: %w", filename, err)
		}
	}

	if err := tw.Close(); err != nil {
		return nil, fmt.Errorf("closing tar writer: %w", err)
	}

	return tarball.LayerFromReader(&buf)
}

// StreamingSplitGGUFLayer creates a streaming tar layer for a GGUF shard.
// The shard consists of a pre-built header (headerBytes) followed by tensor data
// streamed from body. The tar entry is placed at {modelDir}/{filename} with mode 0644.
// Total layer size is len(headerBytes) + bodySize.
func StreamingSplitGGUFLayer(headerBytes []byte, body io.ReadCloser, bodySize int64, modelDir, filename string) v1.Layer {
	return streamTarLayer(path.Join(modelDir, filename), int64(len(headerBytes))+bodySize, headerBytes, body)
}

// StreamingWeightLayer creates a streaming tar layer from a reader.
// The tar entry is placed at {modelDir}/{filename} with mode 0644.
// The reader is consumed lazily as the layer is uploaded, so no temp files are needed.
// It does not close body.
func StreamingWeightLayer(body io.ReadCloser, size int64, modelDir, filename string) v1.Layer {
	return streamTarLayer(path.Join(modelDir, filename), size, nil, io.NopCloser(body))
}

// streamTarLayer streams a single-entry tar layer holding prefix followed by
// body, then closes body.
func streamTarLayer(name string, size int64, prefix []byte, body io.ReadCloser) v1.Layer {
	pr, pw := io.Pipe()

	go func() {
		defer body.Close()
		tw := tar.NewWriter(pw)
		hdr := &tar.Header{
			Name: name,
			Mode: 0o644,
			Size: size,
		}
		if err := tw.WriteHeader(hdr); err != nil {
			pw.CloseWithError(fmt.Errorf("writing tar header: %w", err))
			return
		}
		if _, err := tw.Write(prefix); err != nil {
			pw.CloseWithError(fmt.Errorf("writing GGUF header bytes: %w", err))
			return
		}
		buf := make([]byte, 4<<20) // 4MB buffer to reduce context-switching on fast links
		if _, err := io.CopyBuffer(tw, body, buf); err != nil {
			pw.CloseWithError(fmt.Errorf("copying file data: %w", err))
			return
		}
		if err := tw.Close(); err != nil {
			pw.CloseWithError(fmt.Errorf("closing tar: %w", err))
			return
		}
		pw.Close()
	}()

	return stream.NewLayer(pr)
}
