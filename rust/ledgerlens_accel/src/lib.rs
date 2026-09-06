use pyo3::prelude::*;
use pyo3::types::PyDict;

#[pyfunction]
fn chi_square_batch(_counts: Vec<Vec<u32>>) -> PyResult<Vec<(f64, f64, [f64; 9])>> {
    Ok(vec![])
}

#[pyfunction]
fn tarjan_scc(_adjacency: &Bound<'_, PyDict>) -> PyResult<Vec<Vec<u32>>> {
    Ok(vec![])
}

#[pymodule]
fn ledgerlens_accel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(chi_square_batch, m)?)?;
    m.add_function(wrap_pyfunction!(tarjan_scc, m)?)?;
    Ok(())
}
