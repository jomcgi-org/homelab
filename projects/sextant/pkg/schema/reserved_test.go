package schema_test

import (
	"testing"

	"github.com/jomcgi/homelab/projects/sextant/pkg/schema"
)

// --- CheckFieldName ---

func TestCheckFieldName_ReservedNames(t *testing.T) {
	reserved := []string{"Phase", "Resource", "Validate", "ApplyStatus", "RequeueAfter"}
	for _, name := range reserved {
		err := schema.CheckFieldName(name, "SomeState")
		if err == nil {
			t.Errorf("CheckFieldName(%q) expected error for reserved field, got nil", name)
		}
	}
}

func TestCheckFieldName_GoKeywords(t *testing.T) {
	keywords := []string{"func", "for", "if", "else", "return", "type", "var", "const"}
	for _, kw := range keywords {
		err := schema.CheckFieldName(kw, "SomeState")
		if err == nil {
			t.Errorf("CheckFieldName(%q) expected error for Go keyword, got nil", kw)
		}
	}
}

func TestCheckFieldName_ValidNames(t *testing.T) {
	valid := []string{"resourceID", "resourceName", "tunnelID", "count", "maxRetries"}
	for _, name := range valid {
		err := schema.CheckFieldName(name, "SomeState")
		if err != nil {
			t.Errorf("CheckFieldName(%q) unexpected error: %v", name, err)
		}
	}
}

func TestCheckFieldName_ErrorContainsWord(t *testing.T) {
	err := schema.CheckFieldName("Phase", "MyState")
	if err == nil {
		t.Fatal("expected error")
	}
	rwe, ok := err.(schema.ReservedWordError)
	if !ok {
		t.Fatalf("expected ReservedWordError, got %T", err)
	}
	if rwe.Word != "Phase" {
		t.Errorf("ReservedWordError.Word = %q, want 'Phase'", rwe.Word)
	}
}

// --- CheckFieldGroupName ---

func TestCheckFieldGroupName_GoKeywords(t *testing.T) {
	keywords := []string{"select", "map", "chan", "go"}
	for _, kw := range keywords {
		err := schema.CheckFieldGroupName(kw)
		if err == nil {
			t.Errorf("CheckFieldGroupName(%q) expected error for Go keyword, got nil", kw)
		}
	}
}

func TestCheckFieldGroupName_ValidNames(t *testing.T) {
	valid := []string{"commonFields", "networkData", "storageInfo"}
	for _, name := range valid {
		err := schema.CheckFieldGroupName(name)
		if err != nil {
			t.Errorf("CheckFieldGroupName(%q) unexpected error: %v", name, err)
		}
	}
}

// --- CheckFieldGroupFieldName ---

func TestCheckFieldGroupFieldName_ReservedNames(t *testing.T) {
	err := schema.CheckFieldGroupFieldName("Phase", "myGroup")
	if err == nil {
		t.Error("CheckFieldGroupFieldName('Phase') expected error, got nil")
	}
}

func TestCheckFieldGroupFieldName_GoKeywords(t *testing.T) {
	err := schema.CheckFieldGroupFieldName("return", "myGroup")
	if err == nil {
		t.Error("CheckFieldGroupFieldName('return') expected error, got nil")
	}
}

func TestCheckFieldGroupFieldName_ValidNames(t *testing.T) {
	err := schema.CheckFieldGroupFieldName("tunnelID", "myGroup")
	if err != nil {
		t.Errorf("CheckFieldGroupFieldName('tunnelID') unexpected error: %v", err)
	}
}

// --- CheckStateName ---

func TestCheckStateName_ReservedNames(t *testing.T) {
	err := schema.CheckStateName("Unknown")
	if err == nil {
		t.Error("CheckStateName('Unknown') expected error for reserved state name, got nil")
	}
}

func TestCheckStateName_GoKeywords(t *testing.T) {
	keywords := []string{"func", "type", "interface", "struct"}
	for _, kw := range keywords {
		err := schema.CheckStateName(kw)
		if err == nil {
			t.Errorf("CheckStateName(%q) expected error for Go keyword, got nil", kw)
		}
	}
}

func TestCheckStateName_GoPredeclared(t *testing.T) {
	predeclared := []string{"error", "string", "int", "bool", "nil", "true", "false"}
	for _, name := range predeclared {
		err := schema.CheckStateName(name)
		if err == nil {
			t.Errorf("CheckStateName(%q) expected error for predeclared identifier, got nil", name)
		}
	}
}

func TestCheckStateName_ValidNames(t *testing.T) {
	valid := []string{"Pending", "Ready", "Failed", "Creating", "Deleting"}
	for _, name := range valid {
		err := schema.CheckStateName(name)
		if err != nil {
			t.Errorf("CheckStateName(%q) unexpected error: %v", name, err)
		}
	}
}

// --- CheckActionName ---

func TestCheckActionName_ReservedNames(t *testing.T) {
	err := schema.CheckActionName("isState")
	if err == nil {
		t.Error("CheckActionName('isState') expected error for reserved action name, got nil")
	}
}

func TestCheckActionName_GoKeywords(t *testing.T) {
	keywords := []string{"goto", "defer", "select"}
	for _, kw := range keywords {
		err := schema.CheckActionName(kw)
		if err == nil {
			t.Errorf("CheckActionName(%q) expected error for Go keyword, got nil", kw)
		}
	}
}

func TestCheckActionName_ValidNames(t *testing.T) {
	valid := []string{"MarkReady", "StartCreation", "MarkFailed", "BeginDeletion"}
	for _, name := range valid {
		err := schema.CheckActionName(name)
		if err != nil {
			t.Errorf("CheckActionName(%q) unexpected error: %v", name, err)
		}
	}
}

// --- CheckTransitionParamName ---

func TestCheckTransitionParamName_ReservedNames(t *testing.T) {
	err := schema.CheckTransitionParamName("Phase", "StartCreation")
	if err == nil {
		t.Error("CheckTransitionParamName('Phase') expected error, got nil")
	}
}

func TestCheckTransitionParamName_GoKeywords(t *testing.T) {
	err := schema.CheckTransitionParamName("import", "SomeAction")
	if err == nil {
		t.Error("CheckTransitionParamName('import') expected error for Go keyword, got nil")
	}
}

func TestCheckTransitionParamName_ValidNames(t *testing.T) {
	err := schema.CheckTransitionParamName("tunnelID", "StartCreation")
	if err != nil {
		t.Errorf("CheckTransitionParamName('tunnelID') unexpected error: %v", err)
	}
}

// --- CheckGuardName ---

func TestCheckGuardName_GoKeywords(t *testing.T) {
	keywords := []string{"case", "default", "break", "continue"}
	for _, kw := range keywords {
		err := schema.CheckGuardName(kw)
		if err == nil {
			t.Errorf("CheckGuardName(%q) expected error for Go keyword, got nil", kw)
		}
	}
}

func TestCheckGuardName_ValidNames(t *testing.T) {
	valid := []string{"isReady", "hasRetries", "canDelete"}
	for _, name := range valid {
		err := schema.CheckGuardName(name)
		if err != nil {
			t.Errorf("CheckGuardName(%q) unexpected error: %v", name, err)
		}
	}
}

// --- CheckMetadataName ---

func TestCheckMetadataName_Empty(t *testing.T) {
	err := schema.CheckMetadataName("")
	if err == nil {
		t.Error("CheckMetadataName('') expected error for empty name, got nil")
	}
}

func TestCheckMetadataName_Lowercase(t *testing.T) {
	err := schema.CheckMetadataName("myResource")
	if err == nil {
		t.Error("CheckMetadataName('myResource') expected error for lowercase first char, got nil")
	}
}

func TestCheckMetadataName_GoKeywords(t *testing.T) {
	err := schema.CheckMetadataName("func")
	if err == nil {
		t.Error("CheckMetadataName('func') expected error for Go keyword, got nil")
	}
}

func TestCheckMetadataName_GoPredeclared(t *testing.T) {
	err := schema.CheckMetadataName("Error")
	if err == nil {
		t.Error("CheckMetadataName('Error') expected error for predeclared identifier 'error', got nil")
	}
}

func TestCheckMetadataName_ValidNames(t *testing.T) {
	valid := []string{"CloudflareTunnel", "ModelCache", "MyResource"}
	for _, name := range valid {
		err := schema.CheckMetadataName(name)
		if err != nil {
			t.Errorf("CheckMetadataName(%q) unexpected error: %v", name, err)
		}
	}
}

// --- ReservedWordError ---

func TestReservedWordError_Format(t *testing.T) {
	err := schema.ReservedWordError{
		Word:     "Phase",
		Location: "state \"Pending\" field",
		Reason:   "Phase() method is generated for all states",
	}
	msg := err.Error()
	if msg == "" {
		t.Error("ReservedWordError.Error() should not be empty")
	}
	// Should contain the word
	if len(msg) < 5 {
		t.Errorf("ReservedWordError.Error() too short: %q", msg)
	}
}
