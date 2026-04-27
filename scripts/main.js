let fullData = [];
  Object.values(filters).forEach(filter => {
    filter.addEventListener("change", applyFilters);
  });

  downloadBtn.addEventListener("click", downloadCSV);
}

function applyFilters() {
  filteredData = fullData.filter(row => {
    return (
      (!filters.party.value || row.party === filters.party.value) &&
      (!filters.chamber.value || row.chamber === filters.chamber.value) &&
      (!filters.year.value || row.year === filters.year.value) &&
      (!filters.policy.value || row.policyclaiming === filters.policy.value)
    );
  });

  updateView();
}

function updateView() {
  summary.textContent = `Matching rows: ${filteredData.length}`;
  renderTable();
  renderChart();
}

function renderTable() {
  previewTable.innerHTML = "";

  if (!filteredData.length) return;

  const columns = ["speaker", "party", "chamber", "year", "policyclaiming"];

  const headerRow = document.createElement("tr");
  columns.forEach(col => {
    const th = document.createElement("th");
    th.textContent = col;
    headerRow.appendChild(th);
  });
  previewTable.appendChild(headerRow);

  filteredData.slice(0, 20).forEach(row => {
    const tr = document.createElement("tr");
    columns.forEach(col => {
      const td = document.createElement("td");
      td.textContent = row[col] || "";
      tr.appendChild(td);
    });
    previewTable.appendChild(tr);
  });
}

function renderChart() {
  const counts = {};

  filteredData.forEach(row => {
    const key = row.policyclaiming || "Unknown";
    counts[key] = (counts[key] || 0) + 1;
  });

  const labels = Object.keys(counts);
  const values = Object.values(counts);

  if (chart) chart.destroy();

  chart = new Chart(document.getElementById("resultsChart"), {
    type: "bar",
    data: {
      labels,
      datasets: [{
        label: "Policy Claiming Count",
        data: values
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false
    }
  });
}

function downloadCSV() {
  const csv = Papa.unparse(filteredData);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);

  const link = document.createElement("a");
  link.href = url;
  link.download = "filtered_data.csv";
  link.click();
}
