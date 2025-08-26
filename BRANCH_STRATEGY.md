# tostools Branch Strategy & Workflow

## 🎯 **Parallel Development Strategy**

This document outlines our approach for managing parallel development between operational improvements (master) and architectural revision (major-revision-v0.3.0).

## 📊 **Branch Structure**

### **`master`** - Production Operations
- **Purpose**: Stable, operational GPS metadata processing tool
- **Focus**: Feature additions, bug fixes, operational improvements
- **Users**: Daily GPS processing workflows, GAMIT integration, site log generation
- **Release Cycle**: Continuous deployment of tested features

### **`major-revision-v0.3.0`** - Architectural Modernization  
- **Purpose**: Complete architectural overhaul with modern Python practices
- **Focus**: TOS API redesign, unit testing, clean architecture, performance
- **Users**: Development and testing, future operational replacement
- **Release Cycle**: Major version release when architecture complete

## 🔄 **Quick Branch Switching Commands**

### **Switch to Master (Operations)**
```bash
# Quick switch to master
git checkout master
git pull origin master

# Alternative: Create alias for faster switching
git config alias.to-master 'checkout master'
git to-master
```

### **Switch to Revision (Architecture)**  
```bash
# Quick switch to major revision
git checkout major-revision-v0.3.0  
git pull origin major-revision-v0.3.0

# Alternative: Create alias
git config alias.to-revision 'checkout major-revision-v0.3.0'
git to-revision
```

### **Status Check (Which Branch?)**
```bash
# Quick status
git branch --show-current
git status

# Alternative: Enhanced prompt
# Add to .bashrc/.zshrc: 
# export PS1='[\u@\h \W$(git branch --show-current 2>/dev/null | sed "s/^/ (/" | sed "s/$/)/")]$ '
```

## 📋 **Cross-Branch Workflow for New Use Cases**

### **When New Requirements Arrive:**

#### **1. Impact Assessment**
```bash
# Document new requirement
echo "NEW REQUIREMENT: [description]" >> REQUIREMENTS_TRACKER.md
echo "Date: $(date)" >> REQUIREMENTS_TRACKER.md
echo "Complexity: [LOW/MEDIUM/HIGH]" >> REQUIREMENTS_TRACKER.md
echo "Architecture Impact: [MINIMAL/MODERATE/SIGNIFICANT]" >> REQUIREMENTS_TRACKER.md
```

#### **2. Dual Implementation Strategy**

**For LOW/MEDIUM Impact Requirements:**
```bash
# 1. Implement in master first (operational need)
git checkout master
# ... implement feature ...
git commit -m "feat: [description] - operational implementation"

# 2. Cherry-pick or reimplement in revision branch
git checkout major-revision-v0.3.0
git cherry-pick <commit-hash>  # if compatible
# OR reimplement with clean architecture
```

**For HIGH Impact Requirements:**
```bash
# 1. Prototype in revision branch first (design validation)
git checkout major-revision-v0.3.0  
# ... prototype with clean architecture ...
git commit -m "prototype: [description] - architecture validation"

# 2. Backport simplified version to master
git checkout master
# ... implement simplified operational version ...
git commit -m "feat: [description] - backported from v0.3.0 prototype"
```

## 🔄 **Sync Strategy for Design Decisions**

### **Weekly Architecture Review**
```bash
# Create weekly sync branch
git checkout major-revision-v0.3.0
git checkout -b weekly-sync-$(date +%Y%m%d)

# Compare architectures
git log master --oneline --since="1 week ago" > master-changes.txt
git log major-revision-v0.3.0 --oneline --since="1 week ago" > revision-changes.txt

# Document architectural decisions
echo "## Weekly Architecture Review - $(date +%Y-%m-%d)" >> ARCHITECTURE_DECISIONS.md
echo "### Master Branch Changes:" >> ARCHITECTURE_DECISIONS.md
cat master-changes.txt >> ARCHITECTURE_DECISIONS.md
echo "### Revision Branch Changes:" >> ARCHITECTURE_DECISIONS.md  
cat revision-changes.txt >> ARCHITECTURE_DECISIONS.md
echo "### Design Impact Assessment:" >> ARCHITECTURE_DECISIONS.md
echo "- [Decision 1 and rationale]" >> ARCHITECTURE_DECISIONS.md
```

### **Feature Decision Matrix**

| Requirement Type | Master Implementation | Revision Implementation | Sync Strategy |
|-----------------|----------------------|------------------------|---------------|
| **Bug Fixes** | ✅ Immediate | ⏳ During next sync | Cherry-pick |
| **Small Features** | ✅ Direct implementation | ⏳ Clean rewrite | Design review |
| **Major Features** | ⏳ Prototype first in revision | ✅ Full implementation | Architecture validation |
| **API Changes** | ⚠️ Minimal/compatibility only | ✅ Full redesign | Design decision |

## 🛠️ **Practical Workflow Scripts**

### **Create Handy Aliases**
```bash
# Add to .bashrc/.zshrc or run manually
git config alias.to-master 'checkout master'
git config alias.to-revision 'checkout major-revision-v0.3.0'
git config alias.sync-check '!f() { echo "=== MASTER BRANCH ==="; git checkout master; git log --oneline -5; echo "=== REVISION BRANCH ==="; git checkout major-revision-v0.3.0; git log --oneline -5; }; f'
git config alias.branch-status '!git branch --show-current | sed "s/^/Currently on: /"'

# Usage:
# git to-master
# git to-revision  
# git sync-check
# git branch-status
```

### **New Requirement Workflow**
```bash
#!/bin/bash
# save as: scripts/new-requirement.sh

echo "=== NEW REQUIREMENT ASSESSMENT ==="
echo "Requirement: $1"
echo "Assessment: $2 (LOW/MEDIUM/HIGH impact)"
echo "Date: $(date)" 

# Log the requirement
echo "## $(date +%Y-%m-%d) - $1" >> REQUIREMENTS_TRACKER.md
echo "Impact: $2" >> REQUIREMENTS_TRACKER.md
echo "Status: Evaluating" >> REQUIREMENTS_TRACKER.md
echo "" >> REQUIREMENTS_TRACKER.md

if [ "$2" = "LOW" ] || [ "$2" = "MEDIUM" ]; then
    echo "→ Implement in master first, then sync to revision"
    git checkout master
elif [ "$2" = "HIGH" ]; then
    echo "→ Prototype in revision first, then backport to master"
    git checkout major-revision-v0.3.0
fi

git branch-status
```

## 📊 **Monitoring & Tracking**

### **Files to Create/Maintain:**
- **`REQUIREMENTS_TRACKER.md`** - All new requirements with impact assessment
- **`ARCHITECTURE_DECISIONS.md`** - Weekly sync decisions and rationale
- **`BRANCH_SYNC_LOG.md`** - Record of feature syncs between branches
- **`DESIGN_CHANGES.md`** - Major architecture decisions influenced by operational needs

### **Regular Reviews:**
- **Daily**: Check which branch you're working on before commits
- **Weekly**: Architecture impact review of master branch changes
- **Bi-weekly**: Sync major features between branches
- **Monthly**: Strategic assessment of revision branch progress

## 🎯 **Success Metrics**

- **✅ Operational Continuity**: Master remains stable and feature-rich
- **✅ Architecture Progress**: Revision branch advances toward v0.3.0 goals  
- **✅ Knowledge Transfer**: New requirements inform architectural decisions
- **✅ No Duplicated Work**: Efficient sync between branches
- **✅ Clear Decision Trail**: All architectural choices documented

## 🚀 **Long-term Strategy**

**Phase 1** (Current): Parallel development with cross-pollination
**Phase 2** (Future): Feature freeze on master, focus on revision completion  
**Phase 3** (Migration): Test revision branch extensively with operational workflows
**Phase 4** (Deployment): Replace master with mature v0.3.0 architecture

---

**Current Status**: ✅ Branch created and strategy established  
**Next Steps**: Begin architectural modernization while maintaining operational master